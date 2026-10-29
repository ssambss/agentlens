"""The line at the top of the wall: what panopsis is doing in the background, and how far it got,
without ever saying how long it takes."""

import re

from panopsis import activity
from panopsis.activity import SHOW_AFTER, Jobs


def test_a_job_shows_only_once_it_has_run_for_a_moment(isolated_home):
    jobs = Jobs()
    job = jobs.start("refresh", "refreshing your sessions")
    assert jobs.shown(job.started) == []  # most are done before then: no flicker
    assert jobs.shown(job.started + SHOW_AFTER) == [job]
    jobs.finish("refresh")
    assert jobs.running == {} and jobs.shown(job.started + 10) == []


def test_a_counting_job_fills_its_bar_by_how_many_it_has_done(isolated_home):
    job = Jobs().start("history", "reading your history", total=1072)
    job.done = 536
    assert job.fraction(job.started) == 0.5
    text = activity.line([job], "*", job.started, "blue").plain
    assert text == "* reading your history █████░░░░░ 536 of 1072"


def test_a_paced_job_fills_against_last_time_and_never_says_how_long(isolated_home):
    jobs = Jobs()
    first = jobs.start("usage", "checking your plan usage", paced=True)
    assert first.fraction(first.started + 3) is None  # nothing to go by yet: a spinner only
    first.started -= 4
    jobs.finish("usage")
    # The next launch's goes by it.
    job = Jobs().start("usage", "checking your plan usage", paced=True)
    assert 0.6 < job.fraction(job.started + 4) < 0.7
    assert job.fraction(job.started + 40) < 1  # longer than usual: still going, not done
    text = activity.line([job], "*", job.started + 2, "blue").plain
    assert "checking your plan usage" in text and not re.search(r"\d", text)


def test_only_paced_jobs_are_timed(isolated_home):
    jobs = Jobs()
    jobs.start("refresh", "refreshing your sessions")
    jobs.finish("refresh")  # every few seconds: no file written each time
    assert not activity.durations_path().exists()


def test_the_line_says_what_its_on_and_how_many_more_are_running(isolated_home):
    jobs = Jobs()
    advice = jobs.start("advice", "getting advice")
    advice.steps += ["thinking", "reading code.claude.com/docs/en/hooks"]
    jobs.start("usage", "checking your plan usage")
    text = activity.line(jobs.shown(advice.started + 1), "*", advice.started + 1, "blue").plain
    assert text == "* getting advice: reading code.claude.com/docs/en/hooks  +1"
