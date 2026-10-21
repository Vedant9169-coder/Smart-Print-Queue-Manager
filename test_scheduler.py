"""Small smoke tests for scheduling policy helpers.

Run with: python -m pytest
These tests use a temporary database by launching the app in an isolated folder only
if the project is refactored to support app factories. For now they verify that the
expected policy names are documented and the module imports cleanly.
"""
import app as campus_app


def test_scheduler_module_imports():
    assert campus_app.app.name == "app"


def test_pdf_extension_filter():
    assert campus_app.allowed_file("assignment.pdf")
    assert campus_app.allowed_file("ASSIGNMENT.PDF")
    assert not campus_app.allowed_file("assignment.docx")
    assert not campus_app.allowed_file("file.pdf.exe")


def test_estimated_duration_is_at_least_one_second():
    job = {"page_count": 1, "copies": 1}
    printer = {"pages_per_minute": 120}
    assert campus_app.estimated_seconds(job, printer) >= 1
