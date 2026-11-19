import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from email.message import EmailMessage
from unittest.mock import Mock, patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import update_ips as app


def payload():
    return {
        "code": "200", "statu": "true", "msg": "获取成功",
        "data": {
            "ctcc": {"code": 200, "info": [{"ip": "104.18.36.44"}]},
            "cucc": {"code": 200, "info": [{"ip": "104.17.143.214"}]},
            "cmcc": {"code": 200, "info": [{"ip": "172.66.193.146"}]},
            "bgp": {"code": 200, "info": [{"ip": "104.18.36.44"}]},
            "ipv6": {"code": 200, "info": [{"ip": "2a06:98c1::1"}]},
        },
    }


class SignatureTests(unittest.TestCase):
    def test_real_request_vectors(self):
        for timestamp, expected in (
            (1795117051117, "9e0e39c27d8a704feef81e9ef7db4ebf"),
            (1795117570625, "dc998cca475a7dc8e26ebf982b31b0e1"),
        ):
            self.assertEqual(app.make_key("x64vpYRKglsGquoE", timestamp), expected)

    def test_id_is_read_from_matching_script(self):
        page = '<script src="/other.js?id=wrong"></script><script src="//cdn.example/public/cloudflare.js?v=1&amp;id=new%5Fid"></script>'
        self.assertEqual(app.extract_salt_id(page), "new_id")

    def test_missing_blank_or_conflicting_ids_fail(self):
        for page in (
            '<script src="/cloudflare.js"></script>',
            '<script src="/cloudflare.js?id="></script>',
            '<script src="/cloudflare.js?id=a"></script><script src="/cloudflare.js?id=b"></script>',
        ):
            with self.subTest(page=page), self.assertRaises(app.SourceError):
                app.extract_salt_id(page)


class ParsingTests(unittest.TestCase):
    def test_plain_text_and_html_whitespace(self):
        self.assertEqual(app.parse_plain_ips("\ufeff172.64.229.79,172.64.229.71&#x20;\n"),
                         ["172.64.229.79", "172.64.229.71"])

    def test_invalid_empty_or_error_page_fails(self):
        for text in ("", "<html>error 104.18.36.44</html>", "999.1.2.3", "1.2.3.4:443", "1.2.3.4,invalid", "2a06:98c1::1"):
            with self.subTest(text=text), self.assertRaises(app.SourceError):
                app.parse_plain_ips(text)

    def test_all_ipv4_categories_and_no_ipv6(self):
        self.assertEqual(app.parse_uouin(payload()),
                         ["104.18.36.44", "104.17.143.214", "172.66.193.146", "104.18.36.44"])

    def test_bad_json_shape_status_or_group_fails(self):
        cases = [None, [], {"code": "403"}, {"code": "200", "statu": "false", "data": payload()["data"]}]
        for change in ("missing", "failed", "empty", "bad_ip", "bad_record", "bad_info"):
            data = copy.deepcopy(payload())
            if change == "missing":
                del data["data"]["cucc"]
            elif change == "failed":
                data["data"]["cucc"]["code"] = 500
            elif change == "empty":
                data["data"]["cucc"]["info"] = []
            elif change == "bad_ip":
                data["data"]["cucc"]["info"] = [{"ip": "999.1.2.3"}]
            elif change == "bad_record":
                data["data"]["cucc"]["info"] = [None]
            else:
                data["data"]["cucc"]["info"] = "not a list"
            cases.append(data)
        for data in cases:
            with self.subTest(data=data), self.assertRaises(app.SourceError):
                app.parse_uouin(data)

    def test_stable_deduplication_and_format(self):
        self.assertEqual(app.render_ips(["172.64.229.79", "104.18.36.44", "172.64.229.79"]),
                         "172.64.229.79:443\n104.18.36.44:443\n")
        with self.assertRaises(app.SourceError):
            app.render_ips([])


class CollectionTests(unittest.TestCase):
    def test_http_flow_uses_fresh_page_id_and_signed_api_url(self):
        client = Mock()
        client.get_text.side_effect = [
            "172.64.229.79,104.18.36.44",
            '<script src="/cloudflare.js?id=x64vpYRKglsGquoE"></script>',
            json.dumps(payload()),
        ]
        with patch.object(app.time, "time", return_value=1795117051.117):
            result = app.collect_ips(client)
        self.assertEqual(app.render_ips(result).splitlines(), [
            "172.64.229.79:443", "104.18.36.44:443", "104.17.143.214:443", "172.66.193.146:443",
        ])
        api_call = client.get_text.call_args_list[2]
        query = parse_qs(urlsplit(api_call.args[0]).query)
        self.assertEqual(query["time"], ["1795117051117"])
        self.assertEqual(query["key"], ["9e0e39c27d8a704feef81e9ef7db4ebf"])
        self.assertEqual(api_call.kwargs["referer"], app.UOUIN_PAGE)
        self.assertTrue(api_call.kwargs["api"])

    def test_retry_refreshes_page_id_and_timestamp(self):
        client = Mock()
        client.get_text.side_effect = [
            '<script src="/cloudflare.js?id=first"></script>', '{"code":"403"}',
            '<script src="/cloudflare.js?id=second"></script>', json.dumps(payload()),
        ]
        with patch.object(app.time, "sleep"), patch.object(app.time, "time", side_effect=[1, 2, 3, 4]):
            result = app.retry_source("uouin", lambda: app.fetch_uouin(client))
        self.assertEqual(len(result), 4)
        api_urls = [call.args[0] for call in client.get_text.call_args_list if call.kwargs.get("api")]
        queries = [parse_qs(urlsplit(url).query) for url in api_urls]
        self.assertNotEqual(queries[0]["time"], queries[1]["time"])
        self.assertEqual(queries[1]["key"], [app.make_key("second", int(queries[1]["time"][0]))])

    def test_retry_is_bounded(self):
        operation = Mock(side_effect=app.SourceError("temporary failure"))
        with patch.object(app.time, "sleep"), self.assertRaises(app.SourceError):
            app.retry_source("source", operation)
        self.assertEqual(operation.call_count, 3)


class HttpClientTests(unittest.TestCase):
    def response(self, body, status=200):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.getcode.return_value = status
        response.read.return_value = body
        response.headers = EmailMessage()
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        return response

    def test_api_headers_cookie_handler_timeout_and_json_with_html_content_type(self):
        with patch.object(app, "build_opener") as build:
            client = app.HttpClient()
            build.return_value.open.return_value = self.response(b'\xef\xbb\xbf{"code":"200"}')
            self.assertEqual(client.get_text("https://example.test/api", referer=app.UOUIN_PAGE, api=True), '{"code":"200"}')
            self.assertIsInstance(build.call_args.args[0], app.HTTPCookieProcessor)
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.get_header("Referer"), app.UOUIN_PAGE)
            self.assertEqual(request.get_header("X-requested-with"), "XMLHttpRequest")
            self.assertEqual(request.get_header("Accept-encoding"), "identity")
            self.assertEqual(build.return_value.open.call_args.kwargs["timeout"], 25)

    def test_network_error_bad_status_and_oversize_body_fail(self):
        with patch.object(app, "build_opener") as build:
            client = app.HttpClient()
            build.return_value.open.side_effect = URLError("DNS failed")
            with self.assertRaises(app.SourceError):
                client.get_text("https://example.test")
            build.return_value.open.side_effect = None
            for response in (self.response(b"", 503), self.response(b"x" * (app.MAX_RESPONSE_BYTES + 1))):
                build.return_value.open.return_value = response
                with self.assertRaises(app.SourceError):
                    client.get_text("https://example.test")


class PublicationTests(unittest.TestCase):
    def test_atomic_output_and_identical_content_not_rewritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cloudflare.txt"
            content = "172.64.229.79:443\n"
            self.assertTrue(app.write_output(path, content))
            self.assertEqual(path.read_bytes(), content.encode())
            before = path.stat().st_mtime_ns
            self.assertFalse(app.write_output(path, content))
            self.assertEqual(path.stat().st_mtime_ns, before)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_replace_failure_preserves_original_and_removes_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cloudflare.txt"
            path.write_text("old", encoding="utf-8")
            with patch.object(app.os, "replace", side_effect=OSError("permission denied")), self.assertRaises(OSError):
                app.write_output(path, "new\n")
            self.assertEqual(path.read_text(), "old")
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_failed_collection_does_not_overwrite_and_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cloudflare.txt"
            path.write_text("old", encoding="utf-8")
            errors = io.StringIO()
            with patch.object(app, "collect_ips", side_effect=app.SourceError("source unavailable")), contextlib.redirect_stderr(errors):
                code = app.main(["--output", str(path)])
            self.assertEqual(code, 1)
            self.assertEqual(path.read_text(), "old")
            self.assertIn("source unavailable", errors.getvalue())

    def test_successful_cli_is_silent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cloudflare.txt"
            out, err = io.StringIO(), io.StringIO()
            with patch.object(app, "collect_ips", return_value=["172.64.229.79"]), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(app.main(["--output", str(path)]), 0)
            self.assertEqual(out.getvalue(), "")
            self.assertEqual(err.getvalue(), "")
            self.assertEqual(path.read_bytes(), b"172.64.229.79:443\n")


if __name__ == "__main__":
    unittest.main()
