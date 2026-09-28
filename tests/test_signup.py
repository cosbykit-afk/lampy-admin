"""W-8 signup tests (mocked DB)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest.mock import patch
import signup


def test_validate_ok():
    ok, err = signup.validate_signup("alice_1", "a@example.com",
                                     "password123", "password123")
    assert ok and err is None


def test_validate_bad_username():
    for bad in ["ab", "a"*21, "has space", "has-dash", ""]:
        ok, err = signup.validate_signup(bad, "a@example.com",
                                         "password123", "password123")
        assert not ok and err, bad


def test_validate_bad_email():
    for bad in ["notanemail", "@x.com", "a@b", ""]:
        ok, err = signup.validate_signup("alice", bad,
                                         "password123", "password123")
        assert not ok and err, bad


def test_validate_short_password():
    ok, err = signup.validate_signup("alice", "a@example.com",
                                     "short", "short")
    assert not ok and "8 characters" in err


def test_validate_mismatch():
    ok, err = signup.validate_signup("alice", "a@example.com",
                                     "password123", "different")
    assert not ok and "match" in err


def test_username_taken():
    with patch.object(signup.db, "query", return_value=[{"1": 1}]):
        assert signup.username_taken("Alice") is True
    with patch.object(signup.db, "query", return_value=[]):
        assert signup.username_taken("nobody") is False


def test_create_user_duplicate_username():
    with patch.object(signup, "username_taken", return_value=True):
        try:
            signup.create_user("alice", "a@example.com", "password123")
            assert False, "should raise"
        except ValueError as e:
            assert "username" in str(e).lower()


def test_create_user_duplicate_email():
    with patch.object(signup, "username_taken", return_value=False), \
         patch.object(signup, "email_taken", return_value=True):
        try:
            signup.create_user("alice", "a@example.com", "password123")
            assert False, "should raise"
        except ValueError as e:
            assert "email" in str(e).lower()


def test_create_user_success():
    fake_rows = [{"id": 42, "username": "alice", "is_admin": False}]
    with patch.object(signup, "username_taken", return_value=False), \
         patch.object(signup, "email_taken", return_value=False), \
         patch.object(signup.db, "write", return_value=None), \
         patch.object(signup.db, "query", return_value=fake_rows):
        user = signup.create_user("alice", "a@example.com", "password123")
    assert user == {"id": 42, "username": "alice", "is_admin": False}
