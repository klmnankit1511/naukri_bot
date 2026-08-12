import unittest
from unittest.mock import patch
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from naukri_bot import (
    ANSWER_SYSTEM_PROMPT,
    AnswerBank,
    Job,
    application_success_visible,
    answer_matches_options,
    counts_toward_application_limit,
    configured_experience_answer,
    configured_profile_answer,
    experience_range_option,
    experience_years_from_env,
    freshness_days_from_env,
    max_pages_from_env,
    matches,
    load_history,
    load_config,
    save_history,
    search_url,
    split_answer_choices,
    successful_apply_response,
)


class MatchingTests(unittest.TestCase):
    def test_invalid_config_reports_yaml_location_without_parser_traceback(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text("searches:\n  - keywords: python\nbroken text\n", encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, r"Invalid YAML.*line 4, column 1"):
                load_config(path)

    def test_ai_prompt_contains_approved_affirmative_work_preference(self):
        self.assertIn("answer affirmatively", ANSWER_SYSTEM_PROMPT)
        self.assertIn("hybrid or office work", ANSWER_SYSTEM_PROMPT)
        self.assertIn("Never infer or invent compensation", ANSWER_SYSTEM_PROMPT)

    def test_new_tab_apply_confirmation_text_is_success(self):
        confirmation = 'Applied to "Python Lead"\nStart your interview preparation'
        self.assertTrue(application_success_visible(confirmation.casefold()))

    def test_history_save_creates_single_user_data_directory(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "data" / "history" / "applied_jobs.json"
            expected = {"job": {"status": "applied"}}
            save_history(path, expected)
            self.assertEqual(load_history(path), expected)

    def test_local_timestamp_format_can_be_grouped_by_calendar_day(self):
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        self.assertEqual(timestamp[:10], datetime.now().astimezone().date().isoformat())

    def test_configured_experience_answers_recruiter_question(self):
        with patch.dict("os.environ", {"NAUKRI_EXPERIENCE_YEARS": "3"}):
            self.assertEqual(
                configured_experience_answer(
                    "How many years of experience do you have in Java?"
                ),
                "3",
            )
            self.assertIsNone(configured_experience_answer("What is your current salary?"))

    def test_max_pages_are_read_from_environment(self):
        with patch.dict("os.environ", {"NAUKRI_MAX_PAGES": "3"}):
            self.assertEqual(max_pages_from_env(), 3)

    def test_search_url_uses_page_suffix_after_page_one(self):
        page_one = search_url("Python Development", "", page_number=1)
        page_two = search_url("Python Development", "", page_number=2)
        page_three = search_url("Python Development", "", page_number=3)
        self.assertIn("/python-development-jobs?", page_one)
        self.assertIn("/python-development-jobs-2?", page_two)
        self.assertIn("/python-development-jobs-3?", page_three)

    def test_search_url_uses_canonical_location_and_cplusplus_slug(self):
        self.assertEqual(
            search_url(
                "Embedded Software Engineer Python C++",
                "Bengaluru",
                experience_years=1,
                freshness_days=1,
                page_number=2,
            ),
            "https://www.naukri.com/embedded-software-engineer-python-c-plus-plus-"
            "jobs-in-bengaluru-2?k=embedded%20software%20engineer%20python%20c%2B%2B"
            "&nignbevent_src=jobsearchDeskGNB&experience=1&jobAge=1",
        )

    def test_experience_years_are_read_from_environment(self):
        with patch.dict("os.environ", {"NAUKRI_EXPERIENCE_YEARS": "1"}):
            self.assertEqual(experience_years_from_env(), 1)

    def test_invalid_experience_years_are_rejected(self):
        with patch.dict("os.environ", {"NAUKRI_EXPERIENCE_YEARS": "invalid"}):
            with self.assertRaises(SystemExit):
                experience_years_from_env()

    def test_only_successful_application_counts_toward_limit(self):
        self.assertTrue(counts_toward_application_limit("applied"))
        for status in (
            "skipped",
            "external",
            "already_applied",
            "dry_run",
            "error",
            "needs_input",
            "uncertain",
        ):
            self.assertFalse(counts_toward_application_limit(status), status)

    def test_search_url_includes_freshness(self):
        self.assertIn(
            "jobAge=3",
            search_url("Java Developer", "Bengaluru", experience_years=1, freshness_days=3),
        )

    def test_search_url_matches_naukri_experience_format(self):
        self.assertEqual(
            search_url("Python Development", "", experience_years=1),
            "https://www.naukri.com/python-development-jobs?"
            "k=python%20development&nignbevent_src=jobsearchDeskGNB&experience=1",
        )

    def test_search_url_combines_experience_and_freshness(self):
        self.assertEqual(
            search_url(
                "Python Development", "", experience_years=1, freshness_days=1
            ),
            "https://www.naukri.com/python-development-jobs?"
            "k=python%20development&nignbevent_src=jobsearchDeskGNB"
            "&experience=1&jobAge=1",
        )

    def test_freshness_days_are_read_from_environment(self):
        with patch.dict("os.environ", {"NAUKRI_FRESHNESS_DAYS": "7"}):
            self.assertEqual(freshness_days_from_env(), 7)

    def test_invalid_freshness_days_are_rejected(self):
        with patch.dict("os.environ", {"NAUKRI_FRESHNESS_DAYS": "2"}):
            with self.assertRaises(SystemExit):
                freshness_days_from_env()

    def test_includes_and_excludes(self):
        job = Job("Python Developer", "Acme", "Django API role", "https://example.test/1")
        ok, _ = matches(job, {"include_keywords": ["python"], "exclude_keywords": ["senior"]})
        self.assertTrue(ok)

    def test_exclusion_wins(self):
        job = Job("Senior Python Developer", "Acme", "Django", "https://example.test/2")
        ok, reason = matches(job, {"include_keywords": ["python"], "exclude_keywords": ["senior"]})
        self.assertFalse(ok)
        self.assertIn("senior", reason)

    def test_answer_bank_reuses_equivalent_question(self):
        with TemporaryDirectory() as directory:
            bank = AnswerBank(Path(directory) / "answers.json")
            bank.remember("What is your notice period?", "30 days")
            self.assertEqual(bank.get("What is your NOTICE period"), "30 days")

    def test_answer_bank_retrieves_similar_wording(self):
        with TemporaryDirectory() as directory:
            bank = AnswerBank(Path(directory) / "answers.json")
            bank.remember("How many years of experience do you have in Python development?", "4")
            self.assertEqual(bank.get("How many years experience do you have in Python development?"), "4")

    def test_answer_bank_does_not_mix_skill_experience(self):
        with TemporaryDirectory() as directory:
            bank = AnswerBank(Path(directory) / "answers.json")
            bank.remember("How many years of experience do you have in Python development?", "4")
            self.assertIsNone(bank.get("How many years of experience do you have in PySpark?"))

    def test_experience_maps_to_radio_range(self):
        options = ["No experience", "<4 years", "4-6 years", "6-8 years", ">9 years"]
        question = "How many years of experience do you have in GCP Cloud?"
        self.assertEqual(experience_range_option(question, "3", options), "<4 years")
        self.assertEqual(experience_range_option(question, "4", options), "4-6 years")

    def test_experience_maps_to_different_range_boundary(self):
        options = ["No experience", "<5 years", "5-7 years", "7-9 years", ">12 years"]
        self.assertEqual(
            experience_range_option("How many years of experience do you have in LLM/RAG?", "1", options),
            "<5 years",
        )

    def test_multiple_checkbox_answers_are_resolved(self):
        options = ["Bengaluru, Karnataka", "Chennai, Tamil Nadu", "Hyderabad, Telangana"]
        answer = "Bengaluru, Karnataka ||| Hyderabad, Telangana"
        self.assertEqual(
            split_answer_choices(answer, options),
            ["Bengaluru, Karnataka", "Hyderabad, Telangana"],
        )
        self.assertTrue(answer_matches_options(answer, options))

    def test_comma_inside_option_is_not_split(self):
        options = ["Gurugram, Haryana", "Bengaluru, Karnataka"]
        self.assertEqual(
            split_answer_choices("Bengaluru, Karnataka", options),
            ["Bengaluru, Karnataka"],
        )

    def test_encoded_naukri_success_response(self):
        url = (
            "https://www.naukri.com/myapply/saveApply?"
            "multiApplyResp=%7B%22170726020457%22%3A200%7D"
        )
        self.assertTrue(successful_apply_response(url))
        self.assertFalse(successful_apply_response(url.replace("%3A200", "%3A400")))


if __name__ == "__main__":
    unittest.main()
