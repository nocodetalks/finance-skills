"""`mer.py connect`: keys from .env, sign-in by pasted address, network faked."""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

from helpers import cli

from mer import connect, qbo  # noqa: E402
from mer.util import StopRun  # noqa: E402

CID = "ABcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMN"
SECRET = "s3cretS3cretS3cretS3cretS3cretS3cretS3cr"


class Resp:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, *a):
        return self.body


def http_error(url, code, body):
    return urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(json.dumps(body).encode()))


class FakeIntuit:
    """Answers the token POST and the CompanyInfo GET; records every request."""

    def __init__(self, token_error=None, company="Northwind Labs Inc"):
        self.token_error, self.company, self.calls = token_error, company, []

    def __call__(self, req, timeout=None):
        self.calls.append((req.get_method(), req.full_url, req.data))
        if req.full_url == qbo.TOKEN_URL:
            if self.token_error:
                raise http_error(req.full_url, *self.token_error)
            return Resp({"access_token": "acc", "refresh_token": "ref1", "expires_in": 3600})
        if "/companyinfo/" in req.full_url:
            return Resp({"CompanyInfo": {"CompanyName": self.company, "Country": "US"}})
        raise AssertionError(f"unexpected request {req.full_url}")


class ConnectBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "config"
        os.environ["MER_CONFIG_HOME"] = str(self.home)
        self.root = self.tmp / "month-end"

    def tearDown(self):
        os.environ.pop("MER_CONFIG_HOME", None)
        shutil.rmtree(self.tmp)

    def write_env(self, cid=CID, secret=SECRET, mode=0o600):
        self.root.mkdir(parents=True, exist_ok=True)
        p = self.root / ".env"
        p.write_text(f"# keys\n{connect.ID_KEY}={cid}\n{connect.SECRET_KEY}={secret}\n")
        os.chmod(p, mode)
        return p

    def start(self, **kw):
        with mock.patch("webbrowser.open", return_value=True) as wb:
            msg = connect.start(self.root, "default", kw.pop("environment", "sandbox"), **kw)
        return msg, wb

    def state(self):
        return json.loads(connect.pending_path("default").read_text())["state"]

    def landing(self, realm="9341455", state=None, code="AB11760000000abcdefXYZ"):
        q = urllib.parse.urlencode({"code": code, "state": state or self.state(), "realmId": realm})
        return f"{connect.DEFAULT_REDIRECT}?{q}"


class TestEnvFile(ConnectBase):
    def test_missing_env_creates_private_template_and_stops(self):
        with mock.patch.object(connect, "open_for_editing", return_value=True) as op:
            with self.assertRaises(StopRun) as cm:
                connect.start(self.root, "default", "production")
        p = self.root / ".env"
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        self.assertEqual(connect.read_env(p), {connect.ID_KEY: "", connect.SECRET_KEY: "", "STRIPE_API_KEY": ""})
        self.assertEqual((self.root / ".gitignore").read_text().splitlines()[-1], "*")
        self.assertIn("opened it", str(cm.exception))
        op.assert_called_once_with(p)
        self.assertFalse(connect.pending_path("default").exists())

    def test_existing_folder_gets_no_gitignore(self):
        self.root.mkdir()
        with self.assertRaises(StopRun):
            connect.start(self.root, "default", "production", open_browser=False)
        self.assertFalse((self.root / ".gitignore").exists())

    def test_blank_values_stop_naming_the_key(self):
        self.write_env(secret="")
        with self.assertRaises(StopRun) as cm:
            self.start()
        self.assertIn(connect.SECRET_KEY, str(cm.exception))
        self.assertNotIn(CID, str(cm.exception))

    def test_bad_value_stops_without_echoing_it(self):
        self.write_env(secret="paste your secret here")
        with self.assertRaises(StopRun) as cm:
            self.start()
        self.assertNotIn("paste your secret here", str(cm.exception))
        self.write_env(secret=CID)
        with self.assertRaises(StopRun):
            self.start()

    def test_quotes_export_and_loose_permissions(self):
        p = self.root / ".env"
        self.root.mkdir()
        p.write_text(f'export {connect.ID_KEY}="{CID}"\n{connect.SECRET_KEY} = \'{SECRET}\'\nOTHER=1\n')
        os.chmod(p, 0o644)
        self.start()
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)


class TestStart(ConnectBase):
    def test_opens_sign_in_without_showing_keys(self):
        self.write_env()
        msg, wb = self.start(environment="production")
        url = wb.call_args.args[0]
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertTrue(url.startswith(connect.AUTH_URL))
        self.assertEqual((q["client_id"][0], q["scope"][0], q["redirect_uri"][0], q["response_type"][0]),
                         (CID, connect.SCOPE, connect.DEFAULT_REDIRECT, "code"))
        self.assertEqual(q["state"][0], self.state())
        self.assertNotIn(SECRET, url)
        self.assertNotIn(CID, msg)
        self.assertNotIn(SECRET, msg)
        self.assertEqual(stat.S_IMODE(connect.pending_path("default").stat().st_mode), 0o600)
        self.assertNotIn(SECRET, connect.pending_path("default").read_text())

    def test_no_open_prints_link_but_never_the_secret(self):
        self.write_env()
        with mock.patch("webbrowser.open") as wb:
            msg = connect.start(self.root, "default", "sandbox", open_browser=False)
        wb.assert_not_called()
        self.assertIn(connect.AUTH_URL, msg)
        self.assertNotIn(SECRET, msg)

    def test_environment_required_and_https_for_production(self):
        self.write_env()
        with self.assertRaises(StopRun):
            connect.start(self.root, "default", None)
        with self.assertRaises(StopRun):
            self.start(environment="production", redirect_uri="http://localhost:8000/cb")


class TestParseRedirect(unittest.TestCase):
    def test_direct_landing_page(self):
        got = connect.parse_redirect(f"{connect.DEFAULT_REDIRECT}?code=AB11abc&state=st-1&realmId=9341")
        self.assertEqual(got, {"code": "AB11abc", "realm": "9341", "state": "st-1"})

    def test_bounced_to_intuit_sign_in(self):
        inner = f"{connect.DEFAULT_REDIRECT}?code=AB11abc&state=st_2&realmId=9341"
        outer = ("https://accounts.intuit.com/app/sign-in?redirect_uri=" + urllib.parse.quote(inner, safe="")
                 + "&single_sign_on=false&app_group=ExternalDeveloperPortal")
        self.assertEqual(connect.parse_redirect(outer), {"code": "AB11abc", "realm": "9341", "state": "st_2"})

    def test_code_and_realm_copied_off_the_playground_page(self):
        code = "XAB11791800000HbEanN0fkYMdrjnTSNy9IOZVySgYJMwecXYZ"
        for text in (f"Authorization Code\n{code}\nRealm ID\n9130350000000001", f"{code} 9130350000000001"):
            self.assertEqual(connect.parse_redirect(text), {"code": code, "realm": "9130350000000001"})
        picker = ("https://appcenter.intuit.com/app/connect/oauth2/companyselection?client_id=" + code
                  + "&response_type=code&state=st&locale=en-us")
        self.assertNotIn("code", connect.parse_redirect(picker))   # a client_id in an address is never the code

    def test_error_and_nothing(self):
        self.assertEqual(connect.parse_redirect(f"{connect.DEFAULT_REDIRECT}?error=access_denied&state=x")["error"],
                         "access_denied")
        self.assertEqual(connect.parse_redirect("hello"), {})


class TestFinish(ConnectBase):
    def setUp(self):
        super().setUp()
        self.write_env()

    def finish(self, address, fake=None):
        fake = fake or FakeIntuit()
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            return connect.finish("default", address), fake

    def test_connects_saves_private_creds_and_names_company(self):
        self.start(environment="production")
        msg, fake = self.finish(self.landing())
        creds = qbo.load_creds("default")
        self.assertEqual((creds["client_id"], creds["client_secret"], creds["refresh_token"], creds["realm_id"],
                          creds["environment"]), (CID, SECRET, "ref1", "9341455", "production"))
        self.assertIn("Northwind Labs Inc", msg)
        self.assertNotIn(SECRET, msg)
        self.assertFalse(connect.pending_path("default").exists())
        token_post = fake.calls[0]
        self.assertEqual(token_post[0], "POST")
        form = urllib.parse.parse_qs(token_post[2].decode())
        self.assertEqual((form["grant_type"][0], form["redirect_uri"][0]), ("authorization_code", connect.DEFAULT_REDIRECT))
        self.assertEqual(fake.calls[1][0], "GET")
        self.assertTrue(fake.calls[1][1].startswith(qbo.API_BASE["production"]))

    def test_state_from_another_sign_in_stops(self):
        self.start()
        with self.assertRaises(StopRun):
            self.finish(self.landing(state="someone-elses"))
        self.assertFalse(qbo.cred_path("default").exists())

    def test_denied_or_empty_address_stops(self):
        self.start()
        with self.assertRaises(StopRun) as cm:
            self.finish(f"{connect.DEFAULT_REDIRECT}?error=access_denied&state={self.state()}")
        self.assertIn("access_denied", str(cm.exception))
        with self.assertRaises(StopRun):
            self.finish("https://developer.intuit.com/app/developer/playground")
        picker = ("https://appcenter.intuit.com/app/connect/oauth2/companyselection?client_id=AB1&response_type=code"
                  f"&scope=com.intuit.quickbooks.accounting&state={self.state()}&locale=en-us")
        with self.assertRaises(StopRun) as cm:
            self.finish(picker)
        self.assertIn("company-picker", str(cm.exception))
        self.assertTrue(connect.pending_path("default").exists())     # still waiting for the real address

    def test_expired_code_and_wrong_keys(self):
        self.start()
        with self.assertRaises(StopRun) as cm:
            self.finish(self.landing(), FakeIntuit(token_error=(400, {"error": "invalid_grant"})))
        self.assertIn("expired", str(cm.exception))
        with self.assertRaises(StopRun) as cm:
            self.finish(self.landing(), FakeIntuit(token_error=(401, {"error": "invalid_client"})))
        self.assertIn("Development tab", str(cm.exception))
        self.assertFalse(qbo.cred_path("default").exists())

    def test_no_sign_in_started_or_too_old(self):
        with self.assertRaises(StopRun):
            self.finish(f"{connect.DEFAULT_REDIRECT}?code=AB1&realmId=1")
        self.start()
        pp = connect.pending_path("default")
        data = json.loads(pp.read_text())
        data["started_at"] = "2000-01-01T00:00:00+00:00"
        pp.write_text(json.dumps(data))
        with self.assertRaises(StopRun):
            self.finish(self.landing(state=data["state"]))
        self.assertFalse(pp.exists())

    def test_other_company_on_same_profile_needs_replace(self):
        self.start()
        self.finish(self.landing(realm="111"))
        self.start()
        with self.assertRaises(StopRun) as cm:
            self.finish(self.landing(realm="222"))
        self.assertIn("--replace", str(cm.exception))
        self.assertEqual(qbo.load_creds("default")["realm_id"], "111")
        self.start(replace=True)
        self.finish(self.landing(realm="222"))
        self.assertEqual(qbo.load_creds("default")["realm_id"], "222")
        self.start()
        self.finish(self.landing(realm="222"))        # reconnecting the same company needs nothing extra

    def test_check_reads_company_without_showing_keys(self):
        self.start()
        self.finish(self.landing())
        with mock.patch("urllib.request.urlopen", side_effect=FakeIntuit(company="Northwind Labs Inc")):
            msg = connect.check("default")
        self.assertIn("Northwind Labs Inc", msg)
        self.assertNotIn(SECRET, msg)
        self.assertNotIn("ref1", msg)


class TestCli(ConnectBase):
    def test_cli_paths(self):
        rc, out = cli("connect", "--root", str(self.root), "--no-open")
        self.assertEqual(rc, 2)                       # no .env yet: template made, stop
        self.assertTrue((self.root / ".env").exists())
        rc, out = cli("connect", "--root", str(self.root), "--no-open")
        self.assertEqual(rc, 2)                       # blank keys
        self.write_env()
        rc, out = cli("connect", "--root", str(self.root), "--no-open")
        self.assertEqual(rc, 2)                       # no environment chosen
        rc, out = cli("connect", "--root", str(self.root), "--no-open", "--environment", "sandbox")
        self.assertEqual(rc, 0)
        self.assertNotIn(SECRET, out)
        rc, out = cli("connect", "--check")
        self.assertEqual(rc, 2)                       # not connected yet

    def test_only_one_post_and_it_is_the_token_endpoint(self):
        for mod in (qbo, connect):
            src = Path(mod.__file__).read_text()
            self.assertNotIn('method="PUT"', src)
            self.assertNotIn('method="DELETE"', src)
        self.assertEqual(Path(qbo.__file__).read_text().count('method="POST"'), 1)
        self.assertNotIn("method=", Path(connect.__file__).read_text())


if __name__ == "__main__":
    unittest.main()
