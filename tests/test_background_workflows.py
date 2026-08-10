import json
from pathlib import Path

from openpyxl import load_workbook

from speedy_scraper.background_jobs import clear_stop, create_job, job_is_stale, read_status
from speedy_scraper.company_pocs import build_company_poc_tasks, run_company_poc_job
from speedy_scraper.lead_job import load_lead_job_checkpoint, run_lead_job
from speedy_scraper.models import SearchPage, SearchResult
from speedy_scraper.url_people_job import load_checkpoint_speakers, run_url_people_job


class StopAfterFirstUrlSearch:
    name = "fake"

    def __init__(self, job_dir: Path):
        self.job_dir = job_dir

    def search(self, query, *, max_results, headless=True):
        name = "Asha Rao" if "Asha Rao" in query else "Bina Shah"
        slug = "asha-rao" if name == "Asha Rao" else "bina-shah"
        company = "Razorpay" if name == "Asha Rao" else "PhonePe"
        (self.job_dir / "stop.requested").touch()
        return [
            SearchResult(
                title=f"{name} - CTO - {company} | LinkedIn",
                body=f"{name} is Chief Technology Officer at {company}.",
                href=f"https://www.linkedin.com/in/{slug}/",
                source="fake",
                query=query,
            )
        ]


class ResumeUrlSearch:
    name = "fake"

    def __init__(self):
        self.queries = []

    def search(self, query, *, max_results, headless=True):
        self.queries.append(query)
        return [
            SearchResult(
                title="Bina Shah - CTO - PhonePe | LinkedIn",
                body="Bina Shah is Chief Technology Officer at PhonePe.",
                href="https://www.linkedin.com/in/bina-shah/",
                source="fake",
                query=query,
            )
        ]


def test_url_job_stops_and_resumes_after_checkpoint(tmp_path: Path):
    job_dir = create_job(
        "url_people",
        {"source_url": "https://example.com/people", "sources": ["fake"]},
        jobs_root=tmp_path,
    )
    html = json.dumps(
        {
            "people": [
                {"id": "1", "name": "Asha Rao", "designation": "CTO", "company": "Razorpay"},
                {"id": "2", "name": "Bina Shah", "designation": "CTO", "company": "PhonePe"},
            ]
        }
    )
    first_source = StopAfterFirstUrlSearch(job_dir)
    run_url_people_job(
        job_dir,
        fetcher=lambda _url: html,
        source_builder=lambda _names: [first_source],
    )
    speakers, next_index = load_checkpoint_speakers(job_dir)
    assert read_status(job_dir)["state"] == "paused"
    assert next_index == 1
    assert speakers[0].linkedin_url.endswith("/asha-rao/")

    clear_stop(job_dir)
    resume_source = ResumeUrlSearch()
    run_url_people_job(job_dir, source_builder=lambda _names: [resume_source])
    speakers, next_index = load_checkpoint_speakers(job_dir)
    assert read_status(job_dir)["state"] == "completed"
    assert next_index == 2
    assert len(resume_source.queries) == 1
    assert "Bina Shah" in resume_source.queries[0]
    assert speakers[1].linkedin_url.endswith("/bina-shah/")


class StopAfterFirstPocSearch:
    name = "fake"

    def __init__(self, job_dir: Path, stop: bool):
        self.job_dir = job_dir
        self.stop = stop
        self.queries = []

    def search(self, query, *, max_results, headless=True):
        self.queries.append(query)
        if "Razorpay" in query:
            name, company, slug = "Asha Rao", "Razorpay", "asha-rao"
        else:
            name, company, slug = "Bina Shah", "PhonePe", "bina-shah"
        if self.stop:
            (self.job_dir / "stop.requested").touch()
        return [
            SearchResult(
                title=f"{name} - Chief Technology Officer - {company} | LinkedIn",
                body=f"{company} CTO profile",
                href=f"https://www.linkedin.com/in/{slug}/",
                source="fake",
                query=query,
            )
        ]


def test_company_poc_job_uses_company_role_pairs_and_resumes(tmp_path: Path):
    tasks = build_company_poc_tasks(["Razorpay", "PhonePe"], ["CTO"])
    assert len(tasks) == 2
    assert all("site:linkedin.com/in" in task["query"] for task in tasks)

    job_dir = create_job(
        "company_pocs",
        {
            "companies": ["Razorpay", "PhonePe"],
            "designations": ["CTO"],
            "sources": ["fake"],
            "target_count": 10,
        },
        jobs_root=tmp_path,
    )
    first = StopAfterFirstPocSearch(job_dir, stop=True)
    pocs = run_company_poc_job(job_dir, source_builder=lambda _names: [first])
    assert read_status(job_dir)["state"] == "paused"
    assert [poc.company for poc in pocs] == ["Razorpay"]

    clear_stop(job_dir)
    second = StopAfterFirstPocSearch(job_dir, stop=False)
    pocs = run_company_poc_job(job_dir, source_builder=lambda _names: [second])
    final_status = read_status(job_dir)
    assert final_status["state"] == "completed"
    assert final_status["searches_completed"] == 2
    assert final_status["searches_total"] == 2
    assert len(second.queries) == 1
    assert "PhonePe" in second.queries[0]
    assert {poc.company for poc in pocs} == {"Razorpay", "PhonePe"}


class WrongRoleSource:
    name = "fake"

    def search(self, query, *, max_results, headless=True):
        return [
            SearchResult(
                title="Balaji Bandlapalli - Principal Engineer II - Razorpay | LinkedIn",
                body="Engineering leader at Razorpay.",
                href="https://www.linkedin.com/in/balajibandlapalli/",
                source="fake",
                query=query,
            )
        ]


def test_company_poc_job_rejects_company_match_with_wrong_designation(tmp_path: Path):
    job_dir = create_job(
        "company_pocs",
        {
            "companies": ["Razorpay"],
            "designations": ["Head of Customer Success"],
            "sources": ["fake"],
            "target_count": 10,
        },
        jobs_root=tmp_path,
    )
    pocs = run_company_poc_job(job_dir, source_builder=lambda _names: [WrongRoleSource()])
    assert pocs == []


class BlendedEvidenceSource:
    name = "fake"

    def search(self, query, *, max_results, headless=True):
        return [
            SearchResult(
                title="Balaji Bandlapalli - Principal Engineer II - Razorpay | LinkedIn",
                body=(
                    "Engineering leader at Razorpay. Related result: Jane Doe is the "
                    "Chief Information Officer at another company."
                ),
                href="https://www.linkedin.com/in/balajibandlapalli/",
                source="fake",
                query=query,
            ),
            SearchResult(
                title="Khanjan Desaai - Payments Product - Booking.com | LinkedIn",
                body="Previously built a product at Razorpay. Related profiles include CX leaders.",
                href="https://www.linkedin.com/in/khanjandesai8/",
                source="fake",
                query=query,
            ),
        ]


def test_company_poc_job_rejects_role_or_company_found_only_in_blended_evidence(tmp_path: Path):
    job_dir = create_job(
        "company_pocs",
        {
            "companies": ["Razorpay"],
            "designations": ["CIO"],
            "sources": ["fake"],
            "target_count": 10,
        },
        jobs_root=tmp_path,
    )

    pocs = run_company_poc_job(job_dir, source_builder=lambda _names: [BlendedEvidenceSource()])

    assert pocs == []


def test_job_without_a_live_pid_becomes_recoverable():
    assert job_is_stale(
        {
            "state": "running",
            "pid": 0,
            "updated_at": "2020-01-01T00:00:00+00:00",
        }
    )


class StopAfterLeadSearch:
    name = "fake"

    def __init__(self, job_dir: Path, *, stop: bool):
        self.job_dir = job_dir
        self.stop = stop
        self.calls = 0

    def search(self, query, *, max_results, headless=True):
        self.calls += 1
        if self.stop:
            (self.job_dir / "stop.requested").touch()
        return [
            SearchResult(
                title="Asha Rao - Chief Technology Officer - Razorpay | LinkedIn",
                body="Location: Bengaluru · Razorpay payments platform.",
                href="https://www.linkedin.com/in/asha-rao/",
                source="fake",
                query=query,
            )
        ]


def test_main_lead_job_checkpoints_search_and_resumes_into_verification(tmp_path: Path):
    job_dir = create_job(
        "lead_harvest",
        {
            "roles": ["CTO"],
            "locations": ["Bengaluru"],
            "industries": [],
            "company_names": ["Razorpay"],
            "require_target_company": True,
            "minimum_confidence": 80,
            "sources": ["fake"],
            "target_count": 1,
            "max_queries": 1,
        },
        jobs_root=tmp_path,
    )
    first = StopAfterLeadSearch(job_dir, stop=True)

    run_lead_job(job_dir, source_builder=lambda _names: [first])

    assert read_status(job_dir)["state"] == "paused"
    result, checkpoint = load_lead_job_checkpoint(job_dir)
    assert checkpoint["phase"] == "search"
    assert checkpoint["query_index"] == 1
    assert result.leads == []

    clear_stop(job_dir)
    second = StopAfterLeadSearch(job_dir, stop=False)
    result = run_lead_job(job_dir, source_builder=lambda _names: [second])

    assert second.calls == 0
    final_status = read_status(job_dir)
    assert final_status["state"] == "completed"
    assert [lead.name for lead in result.leads] == ["Asha Rao"]
    workbook = load_workbook(final_status["xlsx_path"], read_only=True)
    assert "Filter Contract" in workbook.sheetnames


class PagedLeadSource:
    name = "paged"

    def __init__(self, job_dir: Path, *, stop_on_first_page: bool):
        self.job_dir = job_dir
        self.stop_on_first_page = stop_on_first_page
        self.pages = []

    def search_page(self, query, *, page, max_results, headless=True):
        self.pages.append(page)
        if page == 1:
            name, company, slug = "Asha Rao", "Razorpay", "asha-rao"
            if self.stop_on_first_page:
                (self.job_dir / "stop.requested").touch()
        else:
            name, company, slug = "Bina Shah", "PhonePe", "bina-shah"
        return SearchPage(
            results=[
                SearchResult(
                    title=f"{name} - Chief Technology Officer - {company} | LinkedIn",
                    body=f"Location: Bengaluru · {company} payments platform.",
                    href=f"https://www.linkedin.com/in/{slug}/",
                    source=self.name,
                    query=query,
                )
            ],
            page=page,
            has_next=page == 1,
        )


def test_main_lead_job_resumes_at_exact_result_page(tmp_path: Path):
    job_dir = create_job(
        "lead_harvest",
        {
            "roles": ["CTO"],
            "locations": ["Bengaluru"],
            "industries": [],
            "company_names": [],
            "require_target_company": False,
            "minimum_confidence": 0,
            "sources": ["paged"],
            "target_count": 2,
            "max_queries": 1,
            "max_results_per_query": 20,
            "max_pages_per_query": 2,
        },
        jobs_root=tmp_path,
    )
    first = PagedLeadSource(job_dir, stop_on_first_page=True)

    run_lead_job(job_dir, source_builder=lambda _names: [first])

    _result, checkpoint = load_lead_job_checkpoint(job_dir)
    assert read_status(job_dir)["state"] == "paused"
    assert first.pages == [1]
    assert checkpoint["query_index"] == 0
    assert checkpoint["page_index"] == 1

    clear_stop(job_dir)
    second = PagedLeadSource(job_dir, stop_on_first_page=False)
    result = run_lead_job(job_dir, source_builder=lambda _names: [second])

    assert second.pages == [2]
    assert {lead.name for lead in result.leads} == {"Asha Rao", "Bina Shah"}
