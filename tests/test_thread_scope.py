"""A thread scope names each subject once (the pull request page asks for
`change=<n>&proposal=<member>…`, and the change already brings its members —
before the dedupe every member thread was listed and counted twice)."""
from plm import threadsapi


def test_a_subject_named_twice_is_listed_once():
    assert threadsapi._scope(proposal="7,7,9") == [("proposal", 7), ("proposal", 9)]
