"""W-4b mail roles tests (mocked DB)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest.mock import patch
import mail_roles


def test_has_admin_true():
    with patch.object(mail_roles.db, "query", return_value=[{"1": 1}]), \
         patch.object(mail_roles, "ensure_table", return_value=None):
        assert mail_roles.has_admin() is True


def test_has_admin_false():
    with patch.object(mail_roles.db, "query", return_value=[]), \
         patch.object(mail_roles, "ensure_table", return_value=None):
        assert mail_roles.has_admin() is False


def test_get_role():
    with patch.object(mail_roles.db, "query",
                      return_value=[{"role": "admin"}]), \
         patch.object(mail_roles, "ensure_table", return_value=None):
        assert mail_roles.get_role("a@b") == "admin"
    with patch.object(mail_roles.db, "query", return_value=[]), \
         patch.object(mail_roles, "ensure_table", return_value=None):
        assert mail_roles.get_role("nobody@b") is None


def test_set_role_valid():
    with patch.object(mail_roles.db, "write", return_value=None), \
         patch.object(mail_roles, "ensure_table", return_value=None):
        mail_roles.set_role("a@b", "admin", created_by="kit")
        mail_roles.set_role("c@d", "moderator")


def test_set_role_invalid():
    try:
        mail_roles.set_role("a@b", "superuser")
        assert False, "should raise"
    except ValueError as e:
        assert "admin or moderator" in str(e)


def test_remove_role():
    with patch.object(mail_roles.db, "write", return_value=None), \
         patch.object(mail_roles, "ensure_table", return_value=None):
        mail_roles.remove_role("a@b")  # should not raise


def test_list_roles():
    rows = [{"james_username": "a@b", "role": "admin"},
            {"james_username": "c@d", "role": "moderator"}]
    with patch.object(mail_roles.db, "query", return_value=rows), \
         patch.object(mail_roles, "ensure_table", return_value=None):
        result = mail_roles.list_roles()
    assert result == [("a@b", "admin"), ("c@d", "moderator")]
