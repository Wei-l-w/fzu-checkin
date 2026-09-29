"""Offline notification tests: SMTP is fully mocked, nothing leaves the machine."""
import contextlib
import io
import smtplib
import unittest
from unittest.mock import patch

from src.notify import notify

QQ = {"type": "email", "email_address": "offline@qq.com", "email_password": "abcdabcdabcdabcd"}


class EmailNotifyTests(unittest.TestCase):
    def setUp(self):
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("no HTTP in email tests"))
        network.start()
        self.addCleanup(network.stop)
        ssl_patch, plain_patch = patch("src.notify.smtplib.SMTP_SSL"), patch("src.notify.smtplib.SMTP")
        self.ssl_cls, self.plain_cls = ssl_patch.start(), plain_patch.start()
        self.addCleanup(ssl_patch.stop)
        self.addCleanup(plain_patch.stop)
        for cls in (self.ssl_cls, self.plain_cls):
            cls.return_value.__enter__.return_value.send_message.return_value = {}

    def test_qq_uses_implicit_tls_and_mails_the_sender_by_default(self):
        self.assertTrue(notify({"notify": dict(QQ)}, "智汇福大晚点名：测试", "正文内容"))
        self.assertEqual(self.ssl_cls.call_args.args[:2], ("smtp.qq.com", 465))
        self.assertEqual(self.ssl_cls.call_args.kwargs["timeout"], 15)
        server = self.ssl_cls.return_value.__enter__.return_value
        server.login.assert_called_once_with("offline@qq.com", "abcdabcdabcdabcd")
        message = server.send_message.call_args.args[0]
        self.assertEqual((message["From"], message["To"]), ("offline@qq.com", "offline@qq.com"))
        self.assertEqual(message["Subject"], "智汇福大晚点名：测试")
        self.assertIn("正文内容", message.get_content())
        self.plain_cls.assert_not_called()

    def test_custom_port_587_uses_starttls_and_an_explicit_recipient(self):
        cfg = {"notify": dict(QQ, email_address="offline@example.edu.cn", email_smtp="smtp.example.edu.cn:587",
                              email_to="other@example.com")}
        self.assertTrue(notify(cfg, "T", "C"))
        self.assertEqual(self.plain_cls.call_args.args[:2], ("smtp.example.edu.cn", 587))
        server = self.plain_cls.return_value.__enter__.return_value
        server.starttls.assert_called_once()
        self.assertEqual(server.send_message.call_args.args[0]["To"], "other@example.com")
        self.ssl_cls.assert_not_called()

    def test_invalid_settings_or_refused_recipients_are_not_success(self):
        for changed in (dict(QQ, email_address="offline@unknown-provider.example"), dict(QQ, email_password=""),
                        dict(QQ, email_to="not-an-address"), dict(QQ, email_smtp="127.0.0.1"),
                        dict(QQ, email_smtp="smtp.qq.com:25")):
            with self.subTest(changed=changed):
                self.assertFalse(notify({"notify": changed}, "T", "C"))
        self.ssl_cls.assert_not_called()
        self.plain_cls.assert_not_called()
        self.ssl_cls.return_value.__enter__.return_value.send_message.return_value = {"offline@qq.com": (550, b"refused")}
        self.assertFalse(notify({"notify": dict(QQ)}, "T", "C"))

    def test_smtp_errors_are_sanitized(self):
        self.ssl_cls.return_value.__enter__.return_value.login.side_effect = smtplib.SMTPAuthenticationError(
            535, b"auth failed abcdabcdabcdabcd")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertFalse(notify({"notify": dict(QQ)}, "T", "C"))
        self.assertNotIn("abcdabcdabcdabcd", output.getvalue())
        self.assertIn("NOTIFY_FAILED", output.getvalue())


if __name__ == "__main__":
    unittest.main()
