# Naukri application assistant

A conservative Playwright bot that searches Naukri, filters visible jobs, and can apply to direct-apply listings. It is intentionally a **dry run by default**.

## Setup

Requires Python 3.10+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp config.example.yaml config.yaml
```

Edit `config.yaml` with your actual roles, locations, and filters. Make sure your Naukri profile and uploaded resume are complete before using the bot.

## Three-person setup

This repository has three independent configurations:

- `config.yaml` — your existing configuration
- `config.person2.yaml` — configuration for person 2
- `config.person3.yaml` — configuration for person 3

Each person has a separate browser session, saved-answer file, application history,
and environment file. Person 1 uses `.env.person1`, person 2 uses `.env.person2`,
and person 3 uses `.env.person3`. Edit the searches, filters, and
`resume_summary` in each file before running it.
Person 2 credentials belong in `.env.person2`, and person 3 credentials belong
in `.env.person3`; these private files are ignored by Git. Leaving a credential
blank makes the bot prompt for it securely in Terminal.

```bash
# You
python naukri_bot.py --config config.yaml

# Person 2
python naukri_bot.py --config config.person2.yaml

# Person 3
python naukri_bot.py --config config.person3.yaml
```

Add `--submit` only after the correct person has logged in and reviewed a dry run. Do not run two instances for the same person at the same time.

## Run safely first

```bash
python naukri_bot.py
```

The browser opens and asks you to log in the first time. Use either guided method:

```bash
# Mobile number + OTP (enter the OTP only in the browser)
python naukri_bot.py --login-mode phone

# Google login
python naukri_bot.py --login-mode google

# Email + password (password input is hidden)
python naukri_bot.py --login-mode email
```

You can optionally provide the phone number through `NAUKRI_MOBILE`. For email login, the bot prompts for `NAUKRI_EMAIL` and a hidden password; credentials are not saved to YAML or application history. Google authorization, OTPs, and CAPTCHAs must be completed by you in the browser. The authenticated session is retained in `browser-profile/`, so later runs normally do not require login. The dry run lists jobs that it *would* apply to without clicking Apply.

If Naukri displays “Something went wrong” while requesting an OTP, the site has rejected that OTP request. The bot automatically opens Google sign-in as a fallback. It uses your installed Google Chrome (`browser_channel: chrome`) to reduce login compatibility problems.

After reviewing the matches, enable submission:

```bash
python naukri_bot.py --submit
```

The bot stops short of answering recruiter questions and does not bypass CAPTCHAs. External company application sites are skipped. Dry-run previews are not written to `applied_jobs.json`; only completed, already-applied, or unsupported jobs are remembered.

## Recruiter follow-up questions

The bot handles Naukri's application drawer one question at a time:

- Saved answers in `answers.json` are reused automatically.
- Strongly similar question wording is matched locally before Azure OpenAI is called, providing a lightweight resume-answer retrieval layer.
- With `OPENAI_API_KEY` set, `gpt-4o-mini` may answer only from the factual `resume_summary` in `config.yaml`.
- Salary, notice period, availability, relocation, authorization, and unsupported questions are always asked in Terminal. Your response is saved locally for future equivalent questions.
- Radio-button questions accept one option number. Checkbox questions accept multiple numbers separated by commas (for example, `1,3`); every selected label is saved together and reused on later applications.
- “Apply on company site” jobs are recorded and skipped without opening the employer website.
- Profile-update interruptions such as “I'll do it later” are dismissed.

Copy the environment template and edit `.env`:

```bash
cp .env.example .env
```

```dotenv
AZURE_OPENAI_API_KEY=your-azure-key
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_API_VERSION=your-supported-api-version
AZURE_OPENAI_DEPLOYMENT=your-gpt-4o-mini-deployment-name
NAUKRI_EMAIL=your-email
NAUKRI_PASSWORD=your-password
NAUKRI_FRESHNESS_DAYS=1
```

`NAUKRI_FRESHNESS_DAYS` controls the Freshness filter for every search. Set it
to `1`, `3`, `7`, `15`, or `30` to select the corresponding “Last N days”
option. Remove the variable or leave it empty to search without a freshness
filter.

`NAUKRI_EXPERIENCE_YEARS` in the selected person's environment file controls
Naukri's experience filter. For example, `NAUKRI_EXPERIENCE_YEARS=1` adds
`experience=1` to every search URL. Person 2 uses `.env.person2`, and person 3
uses `.env.person3`.

`NAUKRI_MAX_PAGES` controls pagination for every configured search. Page 1 uses
the `...-jobs` URL, while later pages use `...-jobs-2`, `...-jobs-3`, and so on.
Pagination stops at this limit, when Naukri returns an empty page, or when the
successful-application limit is reached.

To automatically try freshness windows in order (`1`, `3`, `7`, then `15` days)
until a profile reaches 30 successful applications, run:

```bash
chmod +x run_profile_windows.sh
./run_profile_windows.sh config.person3.yaml email
./run_profile_windows.sh config.person2.yaml email
```

If no argument is supplied, the script defaults to `config.person3.yaml`.

Set `NAUKRI_TARGET_APPLICATIONS` to change the target. The runner counts the
profile's existing `applied` history, so rerunning it does not restart at zero.

Then run:

```bash
python naukri_bot.py --login-mode email --submit
```

When all four Azure values are present, the bot uses `AzureOpenAI` and sends the deployment name as the model identifier. `OPENAI_API_KEY` remains an optional standard-OpenAI fallback. `.env` is excluded by `.gitignore`. Do not paste credentials into `config.yaml` or commit the `.env` file. Naukri credentials are optional; when omitted, the bot prompts at runtime. You can also run without an AI key; unknown questions will simply be asked in Terminal.

## Important

- Start with `max_jobs_per_run: 2` and watch the browser.
- `max_jobs_per_run` counts only newly successful applications. Skipped,
  external, already-applied, failed, and dry-run jobs do not consume the limit.
- Review every filter carefully; automated applications can be low quality or unintended.
- Website markup changes. If no cards are found, update selectors in `collect_jobs()`.
- Use only on your own account and follow Naukri's current terms and rate limits.

Search and job-detail pages use condition-based waits rather than fixed delays. Skeleton/shimmer placeholders are treated as loading, not as zero results. Configure `search_load_timeout_seconds` and `search_load_retries` in `config.yaml` for slower connections.

After clicking Apply—and after each questionnaire Save—the bot waits up to `post_apply_timeout_seconds` for the next drawer message or completion state. This defaults to 30 seconds and does not add an unconditional 30-second delay when the next state becomes ready sooner.

On a search or post-Apply timeout, the bot saves both a screenshot and the rendered HTML under `screenshots/`. Playwright already reads the live DOM in the background, so Selenium is not required for this diagnostic capture.
