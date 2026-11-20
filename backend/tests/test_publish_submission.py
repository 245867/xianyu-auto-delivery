import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import opportunity_publisher as publisher
import rate_limiter


class PublishSubmissionTests(unittest.TestCase):
    def test_web_requests_use_web_key_for_query_and_signature(self):
        response = publisher.requests.Response()
        response.status_code = 200
        response._content = b'{"ret":["SUCCESS::ok"]}'
        for api in (publisher.CATEGORY_API, publisher.PUBLISH_API,
                    publisher.LOCATION_API, "mtop.idle.web.xyh.item.list"):
            with self.subTest(api=api), patch.object(publisher.requests, "post", return_value=response) as post:
                publisher._mtop_request("_m_h5_tk=test_123", api, {}, "1.0", "test", "im-key")
                request = post.call_args.kwargs
                params = request["params"]
                self.assertEqual(params["appKey"], "34839810")
                self.assertEqual(params["sign"], publisher.build_sign("34839810", "test", params["t"], "{}"))

    def test_other_requests_keep_the_supplied_key(self):
        response = publisher.requests.Response()
        response.status_code = 200
        response._content = b'{"ret":["SUCCESS::ok"]}'
        with patch.object(publisher.requests, "post", return_value=response) as post:
            publisher._mtop_request("_m_h5_tk=test_123", "test.other.api", {}, "1.0", "test", "im-key")
        self.assertEqual(post.call_args.kwargs["params"]["appKey"], "im-key")

    def test_account_pause_blocks_before_upload(self):
        with (
            patch.object(rate_limiter, "circuit_blocked", return_value=(True, 42)) as blocked,
            patch.object(publisher, "resolve_images") as upload,
        ):
            with self.assertRaises(publisher.PublishPausedError):
                publisher.publish_item("", {"title": "test", "desc": "test"}, "", cookie_id="shop")
            blocked.assert_called_once_with("shop")
            upload.assert_not_called()

    def test_platform_risk_pauses_only_the_publishing_account(self):
        with (
            patch.object(rate_limiter, "circuit_blocked", return_value=(False, 0)),
            patch.object(rate_limiter, "trip_circuit") as trip,
        ):
            with self.assertRaises(publisher.PublishRiskError):
                self.submit(cookie_id="shop", side_effect=publisher.PublishRiskError("risk"))
            trip.assert_called_once()
            self.assertEqual(trip.call_args.args, ("shop",))

    def submit(self, cookie_id="", **response):
        with (
            patch.object(publisher, "resolve_images", return_value=[]),
            patch.object(publisher, "_category_from_item", return_value={"cat_id": "1", "cat_name": "test"}),
            patch.object(publisher, "get_default_location", return_value={}),
            patch.object(publisher, "_build_publish_data", return_value={}),
            patch.object(publisher, "_mtop_call", **response) as submit,
        ):
            try:
                return publisher.publish_item("", {"title": "test", "desc": "test"}, "", cookie_id=cookie_id)
            finally:
                submit.assert_called_once()

    def assert_status(self, expected, **response):
        with self.assertRaises(publisher.PublishError) as caught:
            self.submit(**response)
        self.assertEqual(caught.exception.submission_status, expected)

    def test_missing_item_id_requires_readback(self):
        self.assert_status("submission_unknown", return_value={"data": {}})

    def test_transport_error_requires_readback(self):
        self.assert_status("submission_unknown", side_effect=publisher.PublishTransportError("offline"))

    def test_explicit_rejection_remains_rejected(self):
        self.assert_status("submission_rejected", side_effect=publisher.PublishError("rejected"))

    def test_item_id_preserves_success(self):
        result = self.submit(return_value={"data": {"itemId": "123"}})
        self.assertEqual(result["itemId"], "123")
        self.assertTrue(result["requires_readback"])


if __name__ == "__main__":
    unittest.main()
