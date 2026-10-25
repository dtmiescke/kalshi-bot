import unittest
from unittest.mock import Mock

from bot.contract_pdf import extract_contract_pdf_text


class TestContractPdf(unittest.TestCase):
    def test_rejects_non_https_and_untrusted_hosts_without_request(self):
        get = Mock()
        self.assertIsNone(extract_contract_pdf_text("http://kalshi.com/rules.pdf", get=get))
        self.assertIsNone(extract_contract_pdf_text("https://evil.example/rules.pdf", get=get))
        get.assert_not_called()

    def test_rejects_oversized_pdf(self):
        response = Mock()
        response.headers = {"Content-Length": str(9 * 1024 * 1024)}
        response.iter_content.return_value = []
        response.raise_for_status.return_value = None
        self.assertIsNone(extract_contract_pdf_text(
            "https://kalshi-public-docs.s3.amazonaws.com/rules.pdf", get=Mock(return_value=response)))

    def test_rejects_non_pdf_content(self):
        response = Mock()
        response.headers = {}
        response.iter_content.return_value = [b"not a pdf"]
        response.raise_for_status.return_value = None
        self.assertIsNone(extract_contract_pdf_text(
            "https://kalshi-public-docs.s3.amazonaws.com/rules.pdf", get=Mock(return_value=response)))


if __name__ == "__main__":
    unittest.main()
