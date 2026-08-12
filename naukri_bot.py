#!/usr/bin/env python3
"""Conservative, stateful Naukri job application assistant."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import yaml
from dotenv import load_dotenv
from playwright.async_api import BrowserContext, Error as PlaywrightError, Page, TimeoutError as PlaywrightTimeoutError


JOB_CARD_SELECTOR = "article.jobTuple, .srp-jobtuple-wrapper, .jobTuple, div[class*='jobTuple']"
JOB_LINK_SELECTOR = "a[href*='/job-listings-']"
LOADING_SELECTOR = "[class*='skeleton' i], [class*='shimmer' i], [class*='loading' i]"


@dataclass(frozen=True)
class Job:
    title: str
    company: str
    url: str
    text: str

    @property
    def key(self) -> str:
        return hashlib.sha256(self.url.encode("utf-8")).hexdigest()[:20]


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"Config not found: {path}. Copy config.example.yaml to config.yaml.")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        problem = getattr(exc, "problem", None) or "invalid YAML syntax"
        raise SystemExit(f"Invalid YAML in {path} at{location}: {problem}") from None
    if not data.get("searches"):
        raise SystemExit("config.yaml needs at least one item under 'searches'.")
    return data


def load_history(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_history(path: Path, history: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")


def normalize_question(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


QUESTION_BOILERPLATE = {
    "a", "an", "and", "are", "can", "cloud", "could", "do", "does", "have", "how",
    "in", "is", "many", "mention", "of", "please", "select", "specify", "the",
    "to", "what", "which", "with", "you", "your", "year", "years", "experience",
}


ANSWER_SYSTEM_PROMPT = (
    "Answer a job-application question using only explicit facts in the resume summary. "
    "The candidate has explicitly confirmed that they are comfortable with recruiter "
    "requirements and work arrangements. For questions asking whether they are willing, "
    "comfortable, open, ready, or able to follow a stated arrangement—including hybrid or "
    "office work, shifts, travel, or relocation—answer affirmatively. Use 'Yes' when there "
    "are no options, or the exact affirmative option label when options are supplied. "
    "Never infer or invent compensation, notice period, dates, work authorization, "
    "citizenship, qualifications, or experience. If any such factual answer is unsupported, "
    "return null. For multi-select, return every supported choice as a JSON array. "
    "Return JSON only: {\"answer\": string|string[]|null}."
)


def question_subject_tokens(value: str) -> set[str]:
    return set(normalize_question(value).split()) - QUESTION_BOILERPLATE


class AnswerBank:
    """Local, user-approved answers keyed by normalized question text."""

    def __init__(self, path: Path):
        self.path = path
        self.answers = load_history(path)

    def get(self, question: str) -> str | None:
        normalized = normalize_question(question)
        item = self.answers.get(normalized)
        if isinstance(item, dict) and "answer" in item:
            return str(item["answer"])

        # Retrieve strongly similar saved questions before calling the model.
        query_tokens = question_subject_tokens(normalized)
        best_item = None
        best_score = 0.0
        for key, candidate in self.answers.items():
            if not isinstance(candidate, dict) or "answer" not in candidate:
                continue
            saved_tokens = question_subject_tokens(key)
            if not query_tokens or not saved_tokens:
                continue
            score = len(query_tokens & saved_tokens) / len(query_tokens | saved_tokens)
            if score > best_score:
                best_score = score
                best_item = candidate
        if best_score >= 0.65 and best_item is not None:
            return str(best_item["answer"])
        return None

    def remember(self, question: str, answer: str) -> None:
        self.answers[normalize_question(question)] = {"question": question, "answer": answer}
        save_history(self.path, self.answers)


class AnswerAssistant:
    def __init__(self, config: dict[str, Any]):
        self.model = str(config.get("openai_model", "gpt-4o-mini"))
        self.resume_summary = str(config.get("resume_summary", ""))
        self.azure_deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "").strip()
        azure_required = (
            "AZURE_OPENAI_API_KEY",
            "AZURE_OPENAI_ENDPOINT",
            "AZURE_OPENAI_API_VERSION",
            "AZURE_OPENAI_DEPLOYMENT",
        )
        azure_values = {name: os.environ.get(name, "").strip() for name in azure_required}
        self.azure_enabled = all(azure_values.values())
        self.azure_missing = [name for name, value in azure_values.items() if not value]
        self.enabled = self.azure_enabled or bool(os.environ.get("OPENAI_API_KEY"))

    def _suggest_sync(self, question: str, options: list[str], multi_select: bool) -> str | None:
        if not self.enabled:
            return None
        try:
            messages = [
                    {
                        "role": "system",
                        "content": ANSWER_SYSTEM_PROMPT,
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "resume_summary": self.resume_summary,
                                "question": question,
                                "options": options,
                                "multi_select": multi_select,
                            },
                            ensure_ascii=False,
                        ),
                    },
                ]
            if self.azure_enabled:
                from openai import AzureOpenAI

                client = AzureOpenAI(
                    api_key=os.environ["AZURE_OPENAI_API_KEY"],
                    azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
                    api_version=os.environ["AZURE_OPENAI_API_VERSION"],
                )
                completion = client.chat.completions.create(
                    model=self.azure_deployment,
                    messages=messages,
                    response_format={"type": "json_object"},
                    temperature=0,
                )
                content = completion.choices[0].message.content or "{}"
            else:
                from openai import OpenAI

                response = OpenAI().responses.create(model=self.model, input=messages)
                content = response.output_text
            data = json.loads(content)
            answer = data.get("answer")
            if isinstance(answer, list):
                values = [str(value).strip() for value in answer if str(value).strip()]
                return " ||| ".join(values) if values else None
            if answer is None or str(answer).strip().casefold() in {"", "null", "none"}:
                return None
            return str(answer).strip()
        except Exception as exc:
            print(f"AI suggestion unavailable: {exc}")
            return None

    async def suggest(self, question: str, options: list[str], multi_select: bool = False) -> str | None:
        # These facts must come directly from the user even when a model is configured.
        sensitive = (
            "salary", "compensation", "notice period", "last working", "join",
            "visa", "authoriz", "citizen", "expected ctc", "current ctc",
        )
        if any(term in question.casefold() for term in sensitive):
            return None
        return await asyncio.to_thread(self._suggest_sync, question, options, multi_select)


def matches(job: Job, config: dict[str, Any]) -> tuple[bool, str]:
    haystack = f"{job.title} {job.company} {job.text}".casefold()
    includes = [str(x).casefold() for x in config.get("include_keywords", []) if str(x).strip()]
    excludes = [str(x).casefold() for x in config.get("exclude_keywords", []) if str(x).strip()]
    blocked = next((word for word in excludes if word in haystack), None)
    if blocked:
        return False, f"excluded keyword: {blocked}"
    if includes and not any(word in haystack for word in includes):
        return False, "no include keyword matched"
    return True, "matched"


FRESHNESS_DAY_CHOICES = {1, 3, 7, 15, 30}


def experience_years_from_env() -> int | None:
    raw_value = os.environ.get("NAUKRI_EXPERIENCE_YEARS", "").strip()
    if not raw_value:
        return None
    try:
        years = int(raw_value)
    except ValueError:
        raise SystemExit("NAUKRI_EXPERIENCE_YEARS must be a whole number from 0 to 50.")
    if years < 0 or years > 50:
        raise SystemExit("NAUKRI_EXPERIENCE_YEARS must be a whole number from 0 to 50.")
    return years


def freshness_days_from_env() -> int | None:
    raw_value = os.environ.get("NAUKRI_FRESHNESS_OVERRIDE", "").strip() or os.environ.get(
        "NAUKRI_FRESHNESS_DAYS", ""
    ).strip()
    if not raw_value:
        return None
    try:
        days = int(raw_value)
    except ValueError:
        raise SystemExit("NAUKRI_FRESHNESS_DAYS must be one of: 1, 3, 7, 15, 30.")
    if days not in FRESHNESS_DAY_CHOICES:
        raise SystemExit("NAUKRI_FRESHNESS_DAYS must be one of: 1, 3, 7, 15, 30.")
    return days


def max_pages_from_env() -> int:
    raw_value = os.environ.get("NAUKRI_MAX_PAGES", "1").strip() or "1"
    try:
        pages = int(raw_value)
    except ValueError:
        raise SystemExit("NAUKRI_MAX_PAGES must be a whole number from 1 to 100.")
    if pages < 1 or pages > 100:
        raise SystemExit("NAUKRI_MAX_PAGES must be a whole number from 1 to 100.")
    return pages


def search_url(
    keywords: str,
    location: str,
    experience_years: int | None = None,
    freshness_days: int | None = None,
    page_number: int = 1,
) -> str:
    if page_number < 1:
        raise ValueError("page_number must be at least 1")
    # Match Naukri's canonical path so its React router does not redirect and
    # discard experience/jobAge. In particular, C++ becomes `c-plus-plus`.
    slug_source = keywords.casefold().replace("c++", "c plus plus")
    slug = re.sub(r"[^a-z0-9]+", "-", slug_source).strip("-")
    location_slug = re.sub(r"[^a-z0-9]+", "-", location.casefold()).strip("-")
    path = f"{slug}-jobs"
    if location_slug:
        path += f"-in-{location_slug}"
    page_suffix = "" if page_number == 1 else f"-{page_number}"
    url = (
        f"https://www.naukri.com/{path}{page_suffix}?"
        f"k={quote(keywords.casefold(), safe='')}"
        "&nignbevent_src=jobsearchDeskGNB"
    )
    if experience_years is not None:
        url += f"&experience={experience_years}"
    if freshness_days is not None:
        url += f"&jobAge={freshness_days}"
    return url


async def ensure_login(page: Page, login_mode: str) -> None:
    await page.goto("https://www.naukri.com/", wait_until="domcontentloaded")
    # The React header often appears after DOMContentLoaded. Without this wait,
    # an expired session can briefly look authenticated because Login is not yet rendered.
    await page.wait_for_timeout(2_000)
    login = await first_visible(page.get_by_text(re.compile(r"^login$", re.I)))
    if login is None:
        print("Using the saved Naukri login session.")
        return

    await login.click()
    await page.wait_for_timeout(800)

    if login_mode == "phone":
        otp_login = page.get_by_text(re.compile(r"(login|continue).*(otp)|otp.*(login|continue)", re.I)).first
        if await otp_login.count():
            await otp_login.click()
            await page.wait_for_timeout(500)
        mobile = os.environ.get("NAUKRI_MOBILE") or input("Naukri mobile number: ").strip()
        phone_input = page.locator(
            "input[type='tel'], input[placeholder*='mobile' i], input[placeholder*='phone' i]"
        ).first
        if mobile and await phone_input.count():
            await phone_input.fill(mobile)
            send_otp = page.get_by_role("button", name=re.compile(r"(send|get|continue|login).*otp", re.I)).first
            if await send_otp.count():
                await send_otp.click()
                await page.wait_for_timeout(1500)
        server_error = page.get_by_text(re.compile(r"something went wrong.*try again", re.I)).first
        if await server_error.count() and await server_error.is_visible():
            print("Naukri did not send an OTP. Falling back to Google sign-in in the browser.")
            google = page.get_by_text(re.compile(r"(continue|login|sign in).*google", re.I)).first
            if await google.count() and await google.is_visible():
                await google.click()
            print("Complete Google sign-in; if the popup is blocked, click 'Sign in with Google' manually.")
        else:
            print("Enter the OTP in the browser. The bot never reads or stores it.")
    elif login_mode == "google":
        google = page.get_by_text(re.compile(r"(continue|login|sign in).*google", re.I)).first
        if await google.count():
            await google.click()
        print("Complete Google sign-in in the browser window.")
    elif login_mode == "email":
        email_login = page.get_by_text(re.compile(r"use email to login|login.*email", re.I)).first
        if await email_login.count() and await email_login.is_visible():
            await email_login.click()
            await page.wait_for_timeout(500)
        email = os.environ.get("NAUKRI_EMAIL") or input("Naukri email: ").strip()
        password = os.environ.get("NAUKRI_PASSWORD") or getpass.getpass("Naukri password (hidden): ")
        email_input = page.locator(
            "input[type='email'], input[name*='email' i], input[placeholder*='email' i], "
            "input[placeholder*='username' i]"
        ).first
        password_input = page.locator("input[type='password']").first
        if not await email_input.count() or not await password_input.count():
            raise SystemExit("Could not find Naukri's email/password fields. Try --login-mode manual.")
        await email_input.fill(email)
        await password_input.fill(password)
        password = ""  # Drop the local reference as soon as the field is filled.
        submit_login = page.get_by_role("button", name=re.compile(r"^(login|sign in)$", re.I)).first
        if await submit_login.count():
            await submit_login.click()
        print("Email login submitted. Complete any verification shown in the browser.")
    else:
        print("Choose any Naukri login method in the browser.")

    print("Complete any CAPTCHA yourself; the bot will not bypass it.")
    input("Press Enter here only after your Naukri profile/home page is visible... ")
    await page.goto("https://www.naukri.com/", wait_until="domcontentloaded")
    await page.wait_for_timeout(2_000)
    login = await first_visible(page.get_by_text(re.compile(r"^login$", re.I)))
    if login is not None:
        raise SystemExit("Naukri still appears logged out. Run the bot and try login again.")
    print("Login confirmed; continuing to job search.")


async def collect_jobs(page: Page) -> list[Job]:
    # Job cards have changed class names over time, so use several narrow fallbacks.
    cards = page.locator(JOB_CARD_SELECTOR)
    if await cards.count() == 0:
        cards = page.locator(JOB_LINK_SELECTOR).locator("xpath=ancestor::*[self::article or self::div][1]")
    results: list[Job] = []
    seen: set[str] = set()
    for index in range(min(await cards.count(), 50)):
        card = cards.nth(index)
        link = card.locator("a[href*='/job-listings-']").first
        if await link.count() == 0:
            continue
        url = (await link.get_attribute("href") or "").split("?")[0]
        if not url or url in seen:
            continue
        seen.add(url)
        title = (await link.get_attribute("title") or await link.inner_text()).strip()
        company_locator = card.locator(".comp-name, .companyInfo a, a[class*='comp']").first
        company = (await company_locator.inner_text()).strip() if await company_locator.count() else "Unknown"
        text = (await card.inner_text()).strip()
        results.append(Job(title=title, company=company, url=url, text=text))
    return results


async def wait_for_search_results(page: Page, timeout_ms: int) -> str:
    """Wait for rendered results or a real empty-state; never treat skeletons as empty."""
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    stable_empty_checks = 0
    while asyncio.get_running_loop().time() < deadline:
        cards = await page.locator(JOB_CARD_SELECTOR).count()
        links = await page.locator(JOB_LINK_SELECTOR).count()
        if cards or links:
            # Give React a brief moment to finish populating card text and hrefs.
            await page.wait_for_timeout(700)
            return "results"

        loaders = page.locator(LOADING_SELECTOR)
        visible_loaders = 0
        for index in range(min(await loaders.count(), 30)):
            if await loaders.nth(index).is_visible():
                visible_loaders += 1
                break
        body = (await page.locator("body").inner_text()).casefold()
        empty_state = any(
            marker in body
            for marker in ("no jobs found", "no results found", "we couldn't find", "0 jobs")
        )
        if empty_state and not visible_loaders:
            stable_empty_checks += 1
            if stable_empty_checks >= 3:
                return "empty"
        else:
            stable_empty_checks = 0
        await page.wait_for_timeout(500)
    return "timeout"


async def load_search(page: Page, url: str, timeout_ms: int, retries: int) -> str:
    for attempt in range(retries + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            state = await wait_for_search_results(page, timeout_ms)
        except PlaywrightTimeoutError:
            state = "timeout"
        if state != "timeout":
            return state
        if attempt < retries:
            print(f"Search content still loading after {timeout_ms // 1000}s; reloading ({attempt + 1}/{retries})...")
    return "timeout"


async def wait_for_job_action(page: Page, timeout_ms: int) -> str:
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    while asyncio.get_running_loop().time() < deadline:
        body = (await page.locator("body").inner_text()).casefold()
        if (
            "already applied" in body
            or "application sent" in body
            or await visible_applied_status(page)
        ):
            return "already"
        external = page.get_by_text(re.compile(r"apply on company site", re.I)).first
        if await external.count() and await external.is_visible():
            return "external"
        direct = await find_visible_apply_button(page)
        if direct is not None:
            return "direct"
        await page.wait_for_timeout(500)
    return "timeout"


async def visible_applied_status(page: Page) -> bool:
    """Recognize Naukri's green, non-clickable `Applied` job status."""
    candidates = page.get_by_text(re.compile(r"^applied$", re.I))
    for index in range(await candidates.count()):
        candidate = candidates.nth(index)
        if not await candidate.is_visible():
            continue
        box = await candidate.bounding_box()
        if box and box["width"] > 0 and box["height"] > 0:
            return True
    return False


async def find_visible_apply_button(page: Page):
    # Naukri currently renders normal and sticky Apply buttons with the same ID.
    # Never use `.first`; select the instance that is visible and enabled now.
    candidates = page.locator("button#apply-button, button.apply-button, button")
    for index in range(await candidates.count()):
        candidate = candidates.nth(index)
        if (await candidate.inner_text()).strip().casefold() != "apply":
            continue
        if await candidate.is_visible() and await candidate.is_enabled():
            box = await candidate.bounding_box()
            if box and box["width"] > 0 and box["height"] > 0:
                return candidate
    return None


async def click_apply_and_wait(page: Page, timeout_ms: int) -> str:
    button = await find_visible_apply_button(page)
    if button is None:
        return "missing"
    await button.scroll_into_view_if_needed()
    await page.wait_for_timeout(300)
    # Re-resolve after scrolling because the sticky header may replace the
    # original header button as the visible React instance.
    button = await find_visible_apply_button(page)
    if button is None:
        return "missing"
    await button.hover()
    # Click exactly once. A retry while an undetected questionnaire is open can
    # bypass its mandatory fields and make Naukri reject the application.
    await button.click(timeout=10_000, no_wait_after=True)
    return await wait_for_post_apply_state(page, timeout_ms)


async def first_visible(locator):
    """Return the first visible match, since Naukri often keeps hidden duplicates."""
    for index in range(await locator.count()):
        candidate = locator.nth(index)
        if await candidate.is_visible():
            return candidate
    return None


async def visible_first(page: Page, selector: str):
    locator = page.locator(selector)
    return await first_visible(locator)


async def capture_diagnostic(page: Page, label: str) -> str:
    directory = Path("diagnostics/screenshots")
    directory.mkdir(parents=True, exist_ok=True)
    safe_label = re.sub(r"[^a-z0-9_-]+", "-", label.casefold()).strip("-")[:60]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = directory / f"{stamp}-{safe_label}"
    try:
        await page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
        base.with_suffix(".html").write_text(await page.content(), encoding="utf-8")
        return str(base)
    except Exception as exc:
        print(f"Could not save page diagnostic: {exc}")
        return ""


async def dismiss_profile_prompt(page: Page) -> bool:
    later = page.get_by_text(re.compile(r"^(i['’]?ll do it later|do it later|skip)$", re.I)).first
    if await later.count() and await later.is_visible():
        await later.click()
        await page.wait_for_timeout(500)
        return True
    return False


def application_success_visible(body: str) -> bool:
    return any(
        marker in body
        for marker in (
            "application sent",
            "successfully applied",
            "applied to",
            "thank you for your responses",
        )
    )


def successful_apply_response(url: str) -> bool:
    decoded_url = unquote(url)
    if "/myapply/saveapply" not in decoded_url.casefold():
        return False
    statuses = [int(value) for value in re.findall(r'"[^"?]+"\s*:\s*(\d{3})', decoded_url)]
    return any(200 <= status < 300 and status != 292 for status in statuses)


async def application_succeeded(page: Page, body: str | None = None) -> bool:
    """Recognize both visible confirmations and Naukri's encoded apply result."""
    if body is None:
        body = (await page.locator("body").inner_text()).casefold()
    if application_success_visible(body):
        return True
    return successful_apply_response(page.url)


async def questionnaire_scope(page: Page):
    # Primary path for Naukri's chat-style application drawer.
    message_fields = page.locator(
        "input[placeholder*='message' i]:visible, textarea[placeholder*='message' i]:visible, "
        "[contenteditable='true'][data-placeholder*='message' i]:visible, "
        "[contenteditable='true'][aria-label*='message' i]:visible"
    )
    for field_index in range(await message_fields.count()):
        field = message_fields.nth(field_index)
        fallback_scope = None
        for level in range(1, 16):
            ancestor = field.locator(f"xpath=ancestor::div[{level}]")
            if not await ancestor.count() or not await ancestor.is_visible():
                continue
            save = ancestor.get_by_text(re.compile(r"^save$", re.I)).last
            if await save.count() and await save.is_visible():
                fallback_scope = ancestor
                text = await ancestor.inner_text()
                if any(line.strip().endswith("?") for line in text.splitlines()):
                    return ancestor
        if fallback_scope is not None:
            return fallback_scope

    selectors = (
        "[role='dialog'], [class*='chatbot' i], [class*='drawer' i], "
        "[class*='apply'][class*='modal' i], [class*='apply'][class*='container' i]"
    )
    candidates = page.locator(selectors)
    for index in range((await candidates.count()) - 1, -1, -1):
        item = candidates.nth(index)
        if await item.is_visible() and await item.locator(
            "input, textarea, select, [role='radio'], [role='checkbox']"
        ).count():
            action = item.get_by_text(
                re.compile(r"^(save|next|submit|continue)$", re.I)
            ).last
            if await action.count() and await action.is_visible():
                return item

    # Naukri sometimes uses generated class names with no dialog semantics.
    # Locate the large right-side ancestor containing a field and Save/Next.
    viewport = page.viewport_size or {"width": 1280, "height": 720}
    fields = page.locator(
        "input:visible, textarea:visible, select:visible, [contenteditable='true']:visible, "
        "[role='radio']:visible, [role='checkbox']:visible"
    )
    for field_index in range(await fields.count()):
        field = fields.nth(field_index)
        for level in range(1, 16):
            ancestor = field.locator(f"xpath=ancestor::div[{level}]")
            if not await ancestor.count() or not await ancestor.is_visible():
                continue
            box = await ancestor.bounding_box()
            if not box:
                continue
            right_drawer = (
                box["x"] >= viewport["width"] * 0.42
                and box["width"] >= 280
                and box["height"] >= viewport["height"] * 0.45
            )
            actions = ancestor.get_by_text(
                re.compile(r"^(save|next|submit|continue)$", re.I)
            ).last
            if right_drawer and await actions.count() and await actions.is_visible():
                return ancestor
    return None


async def read_question(scope) -> tuple[str, list[str]]:
    fields = scope.locator(
        "input:visible, textarea:visible, select:visible, [contenteditable='true']:visible, "
        "[role='radio']:visible, [role='checkbox']:visible"
    )
    if await fields.count() == 0:
        return "", []
    field = fields.last
    question = ""
    field_id = await field.get_attribute("id")
    field_type = (await field.get_attribute("type") or "").casefold()
    if field_id and field_type not in {"radio", "checkbox"}:
        label = scope.locator(f"label[for='{field_id}']").first
        if await label.count():
            question = (await label.inner_text()).strip()
    placeholder = (
        await field.get_attribute("placeholder")
        or await field.get_attribute("data-placeholder")
        or ""
    )
    if "message" in placeholder.casefold() or await field.get_attribute("contenteditable") == "true":
        nearest_prompt = await field.evaluate(
            """el => {
                let current = el;
                for (let depth = 0; current && depth < 12; depth++, current = current.parentElement) {
                    for (let sibling = current.previousElementSibling; sibling; sibling = sibling.previousElementSibling) {
                        const text = (sibling.innerText || '').trim();
                        if (!text) continue;
                        const lines = text.split(/\\n+/).map(x => x.trim()).filter(Boolean);
                        for (let i = lines.length - 1; i >= 0; i--) {
                            if (lines[i].length > 7 && !/^(save|next|submit|continue)$/i.test(lines[i])) {
                                return lines[i];
                            }
                        }
                    }
                }
                return '';
            }"""
        )
        if nearest_prompt:
            question = nearest_prompt
    instruction_markers = (
        "thank you for showing interest",
        "kindly answer all the recruiter",
    )
    if any(marker in question.casefold() for marker in instruction_markers):
        question = ""
    # Naukri's chat drawer renders the question as an earlier message rather
    # than a label beside the input. Prefer the last question-like line.
    scope_lines = [line.strip() for line in (await scope.inner_text()).splitlines() if line.strip()]
    prompts = (
        "how many", "what is", "what are", "are you", "do you", "have you",
        "please specify", "please enter", "please select", "mention your", "select your",
        "current location", "full name", "could you",
    )
    candidates = [
        line
        for line in scope_lines
        if line.endswith("?") or line.casefold().startswith(prompts)
    ]
    if candidates and not question:
        question = candidates[-1]
    if not question:
        fallback = (
            await field.get_attribute("placeholder")
            or await field.get_attribute("data-placeholder")
            or ""
        ).strip()
        # A message placeholder is not a recruiter question. Leave it empty so
        # the state poll waits for the delayed prompt bubble.
        question = "" if "message" in fallback.casefold() else fallback

    options: list[str] = []
    radios = scope.locator("input[type='radio']:visible, [role='radio']:visible")
    for index in range(await radios.count()):
        radio = radios.nth(index)
        radio_id = await radio.get_attribute("id")
        label_text = ""
        if await radio.get_attribute("role") == "radio":
            label_text = (
                await radio.get_attribute("aria-label") or await radio.inner_text()
            ).strip()
        if radio_id:
            label = scope.locator(f"label[for='{radio_id}']").first
            if await label.count():
                label_text = (await label.inner_text()).strip()
        if not label_text:
            parent = radio.locator("xpath=ancestor::*[self::label or self::div][1]")
            if await parent.count():
                label_text = (await parent.inner_text()).strip()
        if not label_text:
            label_text = (await radio.get_attribute("value") or "").strip()
        if label_text and label_text not in options:
            options.append(label_text)
    if await radios.count() and not options:
        for line in scope_lines:
            if line.casefold() in {"yes", "no"} and line not in options:
                options.append(line)
    checkboxes = scope.locator("input[type='checkbox']:visible, [role='checkbox']:visible")
    for index in range(await checkboxes.count()):
        checkbox = checkboxes.nth(index)
        checkbox_id = await checkbox.get_attribute("id")
        label_text = ""
        if await checkbox.get_attribute("role") == "checkbox":
            label_text = (
                await checkbox.get_attribute("aria-label") or await checkbox.inner_text()
            ).strip()
        if checkbox_id:
            label = scope.locator(f"label[for='{checkbox_id}']").first
            if await label.count():
                label_text = (await label.inner_text()).strip()
        if not label_text:
            parent = checkbox.locator("xpath=ancestor::*[self::label or self::div][1]")
            if await parent.count():
                label_text = (await parent.inner_text()).strip()
        if not label_text:
            label_text = (await checkbox.get_attribute("value") or "").strip()
        if label_text and label_text not in options:
            options.append(label_text)
    select = scope.locator("select:visible").last
    if await select.count():
        select_options = await select.locator("option").all_text_contents()
        options.extend(x.strip() for x in select_options if x.strip() and "select" not in x.casefold())
    # Some Naukri multiple-choice cards are clickable divs with no input role.
    # The current question is the last prompt in the transcript, so its
    # following short lines up to Save are its visible options.
    if not options and question:
        question_index = -1
        for index, line in enumerate(scope_lines):
            if normalize_question(line) == normalize_question(question):
                question_index = index
        if question_index >= 0:
            for line in scope_lines[question_index + 1:]:
                if line.casefold() in {"save", "next", "submit", "continue"}:
                    break
                if (
                    0 < len(line) <= 100
                    and "type message" not in line.casefold()
                    and not any(marker in line.casefold() for marker in instruction_markers)
                ):
                    options.append(line)
    return question, list(dict.fromkeys(options))


async def fill_answer(scope, answer: str, options: list[str]) -> bool:
    desired_options = split_answer_choices(answer, options)
    checkboxes = scope.locator("input[type='checkbox']:visible, [role='checkbox']:visible")
    if await checkboxes.count():
        desired = {part.casefold() for part in desired_options}
        matched_labels: set[str] = set()
        for index in range(await checkboxes.count()):
            checkbox = checkboxes.nth(index)
            checkbox_id = await checkbox.get_attribute("id")
            label_text = ""
            if await checkbox.get_attribute("role") == "checkbox":
                label_text = (
                    await checkbox.get_attribute("aria-label") or await checkbox.inner_text()
                ).strip()
            if checkbox_id:
                label = scope.locator(f"label[for='{checkbox_id}']").first
                if await label.count():
                    label_text = (await label.inner_text()).strip()
            if not label_text:
                parent = checkbox.locator("xpath=ancestor::*[self::label or self::div][1]")
                if await parent.count():
                    label_text = (await parent.inner_text()).strip()
            normalized_label = label_text.casefold()
            option_match = any(
                wanted == normalized_label
                or wanted in normalized_label
                or normalized_label in wanted
                for wanted in desired
            )
            if option_match:
                matched_labels.add(next(wanted for wanted in desired if (
                    wanted == normalized_label
                    or wanted in normalized_label
                    or normalized_label in wanted
                )))
                checked = await checkbox.get_attribute("aria-checked") == "true"
                if await checkbox.get_attribute("role") != "checkbox":
                    checked = await checkbox.is_checked()
                if not checked:
                    try:
                        if await checkbox.get_attribute("role") == "checkbox":
                            await checkbox.click()
                        else:
                            await checkbox.check()
                    except PlaywrightError:
                        # The native input is sometimes covered by Naukri's
                        # styled checkbox; its associated label remains clickable.
                        if checkbox_id:
                            label = scope.locator(f"label[for='{checkbox_id}']").first
                            if await label.count():
                                await label.click()
                            else:
                                raise
                        else:
                            raise
        if desired and desired.issubset(matched_labels):
            return True
    if options:
        clicked = False
        for option in desired_options:
            option_control = scope.get_by_text(re.compile(rf"^{re.escape(option)}$", re.I)).last
            if await option_control.count() and await option_control.is_visible():
                await option_control.click()
                clicked = True
        if clicked:
            return True
        if len(desired_options) == 1:
            select = scope.locator("select:visible").last
            if await select.count():
                await select.select_option(label=desired_options[0])
                return True
    field = scope.locator(
        "textarea:visible, input:not([type='radio']):not([type='checkbox']):visible, "
        "[contenteditable='true']:visible"
    ).last
    if await field.count():
        await field.fill(answer)
        return True
    return False


async def questionnaire_is_multi_select(scope, question: str) -> bool:
    """Identify controls where more than one visible choice may be selected."""
    if await scope.locator("input[type='checkbox']:visible, [role='checkbox']:visible").count():
        return True
    normalized = question.casefold()
    return any(
        marker in normalized
        for marker in ("select all", "choose all", "select multiple", "one or more", "applicable")
    )


async def questionnaire_action(scope):
    accessible = scope.get_by_role(
        "button", name=re.compile(r"^(save|next|submit|apply|continue)$", re.I)
    ).last
    if await accessible.count() and await accessible.is_visible():
        return accessible

    text_control = scope.get_by_text(
        re.compile(r"^(save|next|submit|apply|continue)$", re.I)
    ).last
    if await text_control.count() and await text_control.is_visible():
        clickable = text_control.locator(
            "xpath=ancestor-or-self::*[self::button or @role='button'][1]"
        )
        if await clickable.count() and await clickable.is_visible():
            return clickable
        return text_control
    return None


def split_answer_choices(answer: str, options: list[str]) -> list[str]:
    normalized = answer.strip().casefold()
    exact = [option for option in options if option.casefold() == normalized]
    if exact:
        return exact
    partial = [
        option
        for option in options
        if normalized and (normalized in option.casefold() or option.casefold() in normalized)
    ]
    if len(partial) == 1 and "|||" not in answer:
        return partial
    requested = [part.strip().casefold() for part in answer.split("|||") if part.strip()]
    resolved: list[str] = []
    for wanted in requested:
        match = next(
            (
                option
                for option in options
                if wanted == option.casefold()
                or wanted in option.casefold()
                or option.casefold() in wanted
            ),
            None,
        )
        if match and match not in resolved:
            resolved.append(match)
    return resolved


def answer_matches_options(answer: str, options: list[str]) -> bool:
    return bool(split_answer_choices(answer, options))


def experience_range_option(question: str, answer: str, options: list[str]) -> str | None:
    if "how many years" not in question.casefold() and "years of experience" not in question.casefold():
        return None
    match = re.search(r"\d+(?:\.\d+)?", answer)
    if not match:
        return next((x for x in options if "no experience" in x.casefold() and answer == "0"), None)
    years = float(match.group(0))
    for option in options:
        normalized = option.casefold().replace("–", "-")
        if "no experience" in normalized and years == 0:
            return option
        range_match = re.search(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)", normalized)
        if range_match and float(range_match.group(1)) <= years <= float(range_match.group(2)):
            return option
        less_match = re.search(r"<\s*(\d+(?:\.\d+)?)", normalized)
        if less_match and years < float(less_match.group(1)):
            return option
        greater_match = re.search(r">\s*(\d+(?:\.\d+)?)", normalized)
        if greater_match and years > float(greater_match.group(1)):
            return option
    return None


def configured_experience_answer(question: str) -> str | None:
    """Use the profile's approved experience value for recruiter experience questions."""
    normalized = question.casefold()
    experience_question = "experience" in normalized and any(
        marker in normalized
        for marker in ("year", "how much", "how many", "total", "relevant")
    )
    if not experience_question:
        return None
    years = experience_years_from_env()
    return str(years) if years is not None else None


def configured_profile_answer(question: str) -> str | None:
    normalized = question.casefold()
    if any(term in normalized for term in ("relocat", "location requirement")):
        return os.environ.get("NAUKRI_RELOCATION_ANSWER", "").strip() or None
    if any(
        term in normalized
        for term in ("how soon can you start", "when can you start", "availability to join")
    ):
        return os.environ.get("NAUKRI_START_AVAILABILITY", "").strip() or None
    return None


def ask_user(question: str, options: list[str], multi_select: bool = False) -> str:
    print(f"\nRecruiter question: {question}")
    if options:
        for index, option in enumerate(options, 1):
            print(f"  {index}. {option}")
        if multi_select:
            print("Choose one or more option numbers separated by commas, for example: 1,3")
    while True:
        answer = input("Your answer (or 'skip job'): ").strip()
        if answer.casefold() == "skip job":
            return ""
        if options:
            if multi_select and re.fullmatch(r"\d+(?:\s*,\s*\d+)*", answer):
                indexes = [int(value.strip()) for value in answer.split(",")]
                if all(1 <= index <= len(options) for index in indexes):
                    return " ||| ".join(options[index - 1] for index in dict.fromkeys(indexes))
            elif answer.isdigit() and 1 <= int(answer) <= len(options):
                return options[int(answer) - 1]
            elif answer_matches_options(answer, options):
                return " ||| ".join(split_answer_choices(answer, options))
            print("Choose valid option number(s) or type an option exactly.")
            continue
        normalized_question = normalize_question(question)
        if "alternate mobile" in normalized_question and "alternate email" in normalized_question:
            has_email = bool(re.search(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b", answer))
            has_phone = bool(re.search(r"\b\d{10}\b", re.sub(r"[ +()-]", "", answer)))
            if answer.casefold() not in {"na", "n/a", "none"} and not (has_email and has_phone):
                print("Enter both an alternate 10-digit mobile number and email, or enter NA.")
                continue
        if answer:
            return answer


async def handle_questionnaire(
    page: Page, bank: AnswerBank, assistant: AnswerAssistant, transition_timeout_ms: int
) -> tuple[str, str]:
    for _ in range(20):
        await page.wait_for_timeout(500)
        if await dismiss_profile_prompt(page):
            continue
        body = (await page.locator("body").inner_text()).casefold()
        if await application_succeeded(page, body) or "already applied" in body:
            return "applied", "application submitted"

        scope = await questionnaire_scope(page)
        if scope is None:
            confirm = page.get_by_role("button", name=re.compile(r"^(submit|confirm)$", re.I)).last
            if await confirm.count() and await confirm.is_visible() and await confirm.is_enabled():
                await confirm.click()
                continue
            # The drawer can disappear before the React confirmation text is
            # painted. Give the URL/result payload a short chance to settle.
            success_deadline = asyncio.get_running_loop().time() + 3
            while asyncio.get_running_loop().time() < success_deadline:
                if await application_succeeded(page):
                    return "applied", "application submitted"
                await page.wait_for_timeout(250)
            diagnostic = await capture_diagnostic(page, "questionnaire-uncertain")
            detail = "no questionnaire or success message was visible"
            if diagnostic:
                detail += f"; diagnostic saved to {diagnostic}"
            return "uncertain", detail

        question, options = await read_question(scope)
        if not question:
            return "needs_input", "questionnaire was visible but its question could not be read"
        multi_select = await questionnaire_is_multi_select(scope, question)
        answer = configured_experience_answer(question)
        source = "profile experience"
        if answer is None:
            answer = configured_profile_answer(question)
            source = "profile preference"
        if answer is None:
            answer = bank.get(question)
            source = "saved"
        if answer is not None and options:
            ranged_answer = experience_range_option(question, answer, options)
            if ranged_answer:
                answer = ranged_answer
        if answer is None:
            answer = await assistant.suggest(question, options, multi_select)
            source = "resume/AI"
        if answer is None or (options and not answer_matches_options(answer, options)):
            answer = await asyncio.to_thread(ask_user, question, options, multi_select)
            source = "user"
            if not answer:
                return "needs_input", "user chose to skip this job"
            bank.remember(question, answer)
        if "how many years" in question.casefold() and not (
            options and answer_matches_options(answer, options)
        ):
            numeric = re.search(r"\d+(?:\.\d+)?", answer)
            if numeric:
                answer = numeric.group(0)
        print(f"Answering from {source}: {question} -> {answer}")
        if not await fill_answer(scope, answer, options):
            await capture_diagnostic(page, "questionnaire-fill-failed")
            return "needs_input", "could not fill the current questionnaire control"
        next_button = await questionnaire_action(scope)
        if next_button is None:
            await capture_diagnostic(page, "questionnaire-action-missing")
            return "needs_input", "answer filled but the next/save button was unavailable"
        enable_deadline = asyncio.get_running_loop().time() + min(5_000, transition_timeout_ms) / 1000
        while not await next_button.is_enabled() and asyncio.get_running_loop().time() < enable_deadline:
            await page.wait_for_timeout(250)
        if not await next_button.is_enabled():
            await capture_diagnostic(page, "questionnaire-action-disabled")
            return "needs_input", "answer filled but the next/save button stayed disabled"
        await next_button.click()
        previous_question = normalize_question(question)
        transition_deadline = asyncio.get_running_loop().time() + transition_timeout_ms / 1000
        while asyncio.get_running_loop().time() < transition_deadline:
            await page.wait_for_timeout(500)
            body = (await page.locator("body").inner_text()).casefold()
            if await application_succeeded(page, body):
                return "applied", "application submitted"
            later = page.get_by_text(re.compile(r"^(i['’]?ll do it later|do it later|skip)$", re.I)).first
            if await later.count() and await later.is_visible():
                break
            new_scope = await questionnaire_scope(page)
            if new_scope is not None:
                new_question, _ = await read_question(new_scope)
                if new_question and normalize_question(new_question) != previous_question:
                    break
        else:
            return "needs_input", "next questionnaire step did not load before timeout"
    return "needs_input", "questionnaire exceeded 20 steps"


async def wait_for_post_apply_state(page: Page, timeout_ms: int) -> str:
    """Wait for success in any tab, a profile prompt, or a recruiter question."""
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    while asyncio.get_running_loop().time() < deadline:
        try:
            # Some direct applications open Naukri's `/myapply/saveApply`
            # confirmation in a new tab. Check every tab before inspecting the
            # original job-detail page for an inline questionnaire.
            for candidate_page in page.context.pages:
                if candidate_page.is_closed():
                    continue
                if candidate_page is not page and "/myapply/" not in candidate_page.url.casefold():
                    continue
                candidate_body = (
                    await candidate_page.locator("body").inner_text(timeout=2_000)
                ).casefold()
                if await application_succeeded(candidate_page, candidate_body):
                    return "applied"
            body = (await page.locator("body").inner_text()).casefold()
            if "application was not accepted due to incomplete information" in body:
                return "incomplete"
            if await visible_applied_status(page):
                return "applied"
            later = page.get_by_text(re.compile(r"^(i['’]?ll do it later|do it later|skip)$", re.I)).first
            if await later.count() and await later.is_visible():
                return "profile_prompt"
            scope = await questionnaire_scope(page)
            if scope is not None:
                question, _ = await read_question(scope)
                if question:
                    return "questionnaire"
        except PlaywrightError:
            # Expected briefly while Naukri replaces the document after Apply.
            pass
        await page.wait_for_timeout(500)
    return "timeout"


async def apply_to_job(
    context: BrowserContext,
    job: Job,
    submit: bool,
    bank: AnswerBank,
    assistant: AnswerAssistant,
    post_apply_timeout_ms: int,
) -> tuple[str, str]:
    existing_pages = set(context.pages)
    page = await context.new_page()
    try:
        detail_timeout_ms = 60_000
        try:
            await page.goto(job.url, wait_until="domcontentloaded", timeout=detail_timeout_ms)
        except PlaywrightTimeoutError:
            # Naukri can keep background requests open even after the usable
            # job page renders. Continue with visible-state detection.
            pass
        action = await wait_for_job_action(page, detail_timeout_ms)
        if action == "timeout":
            return "error", "job detail stayed in loading state for 60s"
        body = (await page.locator("body").inner_text()).casefold()
        if action == "already":
            return "already_applied", "Naukri reports this application already exists"

        if action == "external":
            return "external", "company-site application skipped"

        apply_button = await find_visible_apply_button(page)
        if apply_button is None:
            return "skipped", "no direct Apply button (possibly an external application)"
        if not submit:
            return "dry_run", "would click Apply"

        post_apply_state = await click_apply_and_wait(page, post_apply_timeout_ms)
        if post_apply_state == "applied":
            return "applied", "application submitted"
        if post_apply_state == "missing":
            return "error", "visible Apply button disappeared before it could be clicked"
        if post_apply_state == "incomplete":
            return "needs_input", "Naukri rejected the attempt because mandatory questions were unanswered"
        if post_apply_state == "timeout":
            await capture_diagnostic(page, f"post-apply-timeout-{job.company}-{job.title}")
            return "error", f"nothing appeared within {post_apply_timeout_ms // 1000}s after Apply"
        return await handle_questionnaire(page, bank, assistant, post_apply_timeout_ms)
    except PlaywrightTimeoutError:
        return "error", "page timed out"
    finally:
        # Close the job-detail page and any confirmation popup it opened, but
        # preserve the search page and other tabs that existed before this job.
        for owned_page in list(context.pages):
            if owned_page not in existing_pages and not owned_page.is_closed():
                await owned_page.close()


def counts_toward_application_limit(status: str) -> bool:
    """Only a newly completed application consumes the per-run limit."""
    return status == "applied"


async def run(args: argparse.Namespace) -> int:
    load_dotenv()
    config = load_config(Path(args.config))
    history_path = Path(config.get("history_file", "applied_jobs.json"))
    history = load_history(history_path)
    answer_bank = AnswerBank(Path(config.get("answers_file", "answers.json")))
    answer_assistant = AnswerAssistant(config)
    if answer_assistant.azure_enabled:
        print(f"AI answers enabled through Azure deployment: {answer_assistant.azure_deployment}")
    elif os.environ.get("AZURE_OPENAI_API_KEY"):
        print("Azure AI answers disabled; missing: " + ", ".join(answer_assistant.azure_missing))
    # Older versions incorrectly persisted previewed jobs and then skipped them
    # during real submission runs. Preview records are never completion records.
    history = {
        key: value
        for key, value in history.items()
        if value.get("status") != "dry_run"
    }
    max_jobs = int(os.environ.get("NAUKRI_MAX_JOBS_OVERRIDE", config.get("max_jobs_per_run", 10)))
    search_timeout_ms = int(config.get("search_load_timeout_seconds", 60)) * 1000
    search_retries = int(config.get("search_load_retries", 2))
    post_apply_timeout_ms = int(config.get("post_apply_timeout_seconds", 30)) * 1000
    experience_years = experience_years_from_env()
    if experience_years is not None:
        print(f"Experience filter: {experience_years} year(s)")
    freshness_days = freshness_days_from_env()
    if freshness_days is not None:
        suffix = "" if freshness_days == 1 else "s"
        print(f"Freshness filter: Last {freshness_days} day{suffix}")
    max_pages = max_pages_from_env()
    print(f"Pages per search: {max_pages}")

    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        launch_options: dict[str, Any] = {
            "user_data_dir": str(
                Path(config.get("profile_dir", "data/browser/profile")).resolve()
            ),
            "headless": bool(config.get("headless", False)),
            "slow_mo": int(config.get("slow_mo_ms", 250)),
        }
        browser_channel = str(config.get("browser_channel", "")).strip()
        if browser_channel:
            launch_options["channel"] = browser_channel
        context = await playwright.chromium.launch_persistent_context(**launch_options)
        page = context.pages[0] if context.pages else await context.new_page()
        await ensure_login(page, args.login_mode)
        applied_count = 0
        try:
            for search in config["searches"]:
                if applied_count >= max_jobs:
                    break
                keywords = str(search["keywords"])
                location = str(search.get("location", ""))
                print(f"\nSearching: {keywords} — {location}")
                seen_page_signatures: set[tuple[str, ...]] = set()
                for page_number in range(1, max_pages + 1):
                    if applied_count >= max_jobs:
                        break
                    print(f"PAGE {page_number}/{max_pages}")
                    current_search_url = search_url(
                        keywords,
                        location,
                        experience_years=experience_years,
                        freshness_days=freshness_days,
                        page_number=page_number,
                    )
                    print(f"SEARCH_URL {current_search_url}")
                    state = await load_search(
                        page,
                        current_search_url,
                        search_timeout_ms,
                        search_retries,
                    )
                    if state == "timeout":
                        await capture_diagnostic(
                            page, f"search-timeout-{keywords}-{location}-page-{page_number}"
                        )
                        print(
                            f"LOAD_TIMEOUT Page {page_number} did not finish rendering after "
                            f"{search_retries + 1} attempts; moving to the next search"
                        )
                        break
                    if state == "empty":
                        print(f"Page {page_number} has no jobs; pagination complete")
                        break
                    jobs = await collect_jobs(page)
                    if not jobs:
                        print(f"Page {page_number} has no rendered job cards; pagination complete")
                        break
                    page_signature = tuple(job.key for job in jobs)
                    if page_signature in seen_page_signatures:
                        print(
                            f"Page {page_number} repeats an earlier result page; "
                            "pagination complete"
                        )
                        break
                    seen_page_signatures.add(page_signature)
                    print(f"Found {len(jobs)} rendered job cards on page {page_number}")
                    for job in jobs:
                        if applied_count >= max_jobs:
                            break
                        if job.key in history:
                            previous_status = str(history[job.key].get("status", "processed")).upper()
                            print(f"HISTORY      {job.title} @ {job.company} — {previous_status}")
                            continue
                        accepted, reason = matches(job, config)
                        if not accepted:
                            print(f"SKIP  {job.title} @ {job.company} ({reason})")
                            continue
                        status, detail = await apply_to_job(
                            context,
                            job,
                            args.submit,
                            answer_bank,
                            answer_assistant,
                            post_apply_timeout_ms,
                        )
                        print(f"{status.upper():<12} {job.title} @ {job.company} — {detail}")
                        # Do not suppress future attempts after an error or manual-question stop.
                        if status in {"applied", "already_applied", "skipped", "external"}:
                            history[job.key] = {
                                "title": job.title,
                                "company": job.company,
                                "url": job.url,
                                "status": status,
                                "processed_at": datetime.now().astimezone().isoformat(
                                    timespec="seconds"
                                ),
                            }
                            save_history(history_path, history)
                        if counts_toward_application_limit(status):
                            applied_count += 1
                            print(f"APPLICATION_COUNT {applied_count}/{max_jobs}")
            if applied_count >= max_jobs:
                print(
                    f"\nRUN_LIMIT Reached max_jobs_per_run={max_jobs} successful "
                    "applications; stopping this run."
                )
        finally:
            await context.close()
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Naukri application assistant (dry-run by default)")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--submit", action="store_true", help="allow the bot to click Apply")
    parser.add_argument(
        "--login-mode",
        choices=("phone", "google", "email", "manual"),
        default="manual",
        help="guide the first login using phone OTP, Google, email/password, or manually",
    )
    return parser.parse_args()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(run(parse_args())))
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        raise SystemExit(130)
