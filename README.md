# Naukri application assistant

A conservative Playwright bot that searches Naukri, filters visible jobs, and can apply to direct-apply listings. It is intentionally a **dry run by default**.

## Setup

Requires Python 3.10+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
cp config.example.yaml config.yaml
```

Edit `.env` with the active user's credentials and `config.yaml` with their
roles, locations, filters, and factual resume summary. Make sure the matching
Naukri profile and uploaded resume are complete before using the bot.

## Single-user setup

The bot uses one active-user environment and configuration:

- `.env` contains credentials and environment settings.
- `config.yaml` contains searches, filters, paths, and the resume summary.
- `data/browser/profile/` contains the saved login session.
- `data/history/applied_jobs.json` contains application history.
- `data/answers/answers.json` contains approved recruiter-question answers.

Changing `.env` credentials alone does not replace an already authenticated
browser session. Before switching to another person, stop the bot and move the
existing `data/` directory to a private backup location. Then update `.env` and
`config.yaml` and run a dry run. The bot will create fresh browser, history, and
answer data for the new user. Do not reuse one user's history or saved answers
for another user.

```bash
python naukri_bot.py --config config.yaml
```

Add `--submit` only after the active user has logged in and reviewed a dry run.
Do not run two instances against the same data directory at the same time.

### Generate a configuration from a resume

Pass a user name and a TXT, MD, PDF, or DOCX resume to the generator:

```bash
python scripts/create_user_config.py "User Name" /path/to/resume.pdf
```

The generator creates `config.yaml` when it does not exist. If it already
exists, it creates the next available file such as `config.person2.yaml` or
`config.person3.yaml`. Each generated person config receives separate browser,
history, and answer paths, while credentials still come from the shared `.env`.
Update `.env` for the selected user before running their config. Existing YAML
files are never overwritten. Always review the generated searches, location,
keywords, and resume summary before enabling `--submit`.

You can also request a specific unused output file:

```bash
python scripts/create_user_config.py "User Name" resume.docx --output new-user.yaml
```

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

You can optionally provide the phone number through `NAUKRI_MOBILE`. For email login, the bot prompts for `NAUKRI_EMAIL` and a hidden password; credentials are not saved to YAML or application history. Google authorization, OTPs, and CAPTCHAs must be completed by you in the browser. The authenticated session is retained in `data/browser/profile/`, so later runs normally do not require login. The dry run lists jobs that it *would* apply to without clicking Apply.

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

The AI prompt records the candidate's preference to answer affirmatively when
asked whether they are comfortable, willing, open, ready, or able to follow a
stated work arrangement. It still does not fabricate factual answers such as
experience, compensation, notice period, dates, qualifications, or work
authorization.

`NAUKRI_FRESHNESS_DAYS` controls the Freshness filter for every search. Set it
to `1`, `3`, `7`, `15`, or `30` to select the corresponding “Last N days”
option. Remove the variable or leave it empty to search without a freshness
filter.

`NAUKRI_EXPERIENCE_YEARS` in `.env` controls Naukri's experience filter. For
example, `NAUKRI_EXPERIENCE_YEARS=1` adds `experience=1` to every search URL.

`NAUKRI_MAX_PAGES` controls pagination for every configured search. Page 1 uses
the `...-jobs` URL, while later pages use `...-jobs-2`, `...-jobs-3`, and so on.
Pagination stops at this limit, when Naukri returns an empty page, or when the
successful-application limit is reached.

To automatically try freshness windows in order (`1`, `3`, `7`, then `15` days)
until the active configuration reaches 40 successful applications today, run:

```bash
./scripts/run_profile_windows.sh email
```

If no argument is supplied, the runner uses email login and `config.yaml`.

Set `NAUKRI_TARGET_APPLICATIONS` to change the default daily target of 40. The
counter starts at zero on each new local calendar day. Rerunning on the same day
continues from that day's recorded successes. Older application history remains
available for duplicate prevention but does not count toward today's target.

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

On a search or post-Apply timeout, the bot saves both a screenshot and the rendered HTML under `diagnostics/screenshots/`. Playwright already reads the live DOM in the background, so Selenium is not required for this diagnostic capture.

## Azure trigger API

The container exposes authenticated `POST /1`, `POST /2`, and `POST /3`
endpoints for Ankit, Amisha, and Seema respectively. A request adds the profile
to one serial queue and returns HTTP 202 with a `run_id`; it does not keep the
HTTP request open while Playwright runs. Use `GET /runs/{run_id}` to inspect a
run and `GET /health` for the public health check.

```bash
curl -X POST -H "x-api-key: $API_KEY" https://YOUR-APP.azurewebsites.net/1
curl -H "x-api-key: $API_KEY" https://YOUR-APP.azurewebsites.net/runs/RUN_ID
```

Cloud runs are headless and non-interactive. Unknown recruiter questions are
skipped safely. A new or expired Naukri session can still require CAPTCHA, OTP,
or manual verification; the bot does not bypass those checks. Configure
`PERSON_1_EMAIL`, `PERSON_1_PASSWORD`, and equivalent variables for Persons 2
and 3 as Azure Web App settings. Each profile uses separate persistent storage
under `NAUKRI_DATA_ROOT`.

The deployment workflow builds the Playwright container, pushes it to Azure
Container Registry, and deploys it to Azure Web App. The scheduler workflow
queues `/1`, `/2`, and `/3` in that order every day at 09:00 Asia/Kolkata and
also supports manual dispatch from GitHub Actions.
