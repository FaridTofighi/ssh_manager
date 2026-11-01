"""How a FreeRDP run ended, and which certificates are trusted (pure tables)."""

import unittest

from ophion.rdp.outcome import ERRCONNECT, ERRINFO, EXIT_CODES, Kind, classify_rdp_exit, connected_line
from ophion.rdp.trust_flow import Action, decide
from ophion.trust import CertificateInfo

A = ":".join(["AA"] * 32)
B = ":".join(["BB"] * 32)


def err(token: str) -> str:
    return f"[10:15:40:224] [3699334:00387289] [ERROR][com.freerdp.core] - [freerdp_connect]: {token} [0x0002000D]"


class ExitCodeTests(unittest.TestCase):
    def test_every_exit_code_has_a_kind_and_message(self) -> None:
        for code, (kind, reason, message) in EXIT_CODES.items():
            outcome = classify_rdp_exit(code, [])
            self.assertIsInstance(outcome.kind, Kind)
            self.assertTrue(outcome.message and outcome.reason, code)
        self.assertEqual(classify_rdp_exit(134, []).kind, Kind.AUTH_FAILED)
        self.assertEqual(classify_rdp_exit(135, []).reason, "locked_out")
        self.assertEqual(classify_rdp_exit(158, []).reason, "no_logon_right")
        self.assertEqual(classify_rdp_exit(140, []).kind, Kind.DNS)
        self.assertEqual(classify_rdp_exit(147, []).kind, Kind.NETWORK)
        self.assertEqual(classify_rdp_exit(143, []).kind, Kind.TLS)
        self.assertEqual(classify_rdp_exit(133, []).kind, Kind.SECURITY_MISMATCH)
        self.assertEqual(classify_rdp_exit(128, []).kind, Kind.NEEDS_ENGINE_FEATURE)
        self.assertEqual(classify_rdp_exit(20, []).reason, "license")
        self.assertEqual(classify_rdp_exit(77, []).reason, "protocol")
        self.assertEqual(classify_rdp_exit(200, []).kind, Kind.OTHER)

    def test_after_a_connection_the_code_is_the_server_reason(self) -> None:
        self.assertEqual(classify_rdp_exit(0, [], connected=True).kind, Kind.OK_DISCONNECT)
        logoff = classify_rdp_exit(12, [], connected=True)
        self.assertEqual((logoff.kind, logoff.reason), (Kind.OK_DISCONNECT, "logoff"))
        self.assertEqual(classify_rdp_exit(3, [], connected=True).reason, "idle_timeout")
        self.assertEqual(classify_rdp_exit(5, [], connected=True).reason, "replaced")
        early = classify_rdp_exit(1, [])  # disconnected before the session started
        self.assertEqual(early.kind, Kind.NETWORK)

    def test_signals(self) -> None:
        self.assertEqual(classify_rdp_exit(-9, []).kind, Kind.OTHER)
        dropped = classify_rdp_exit(-11, [], connected=True)
        self.assertTrue(dropped.auto_reconnect)
        mine = classify_rdp_exit(-15, [], stopped_by_user=True, connected=True)
        self.assertEqual((mine.kind, mine.reason), (Kind.OK_DISCONNECT, "user_disconnect"))
        self.assertFalse(mine.is_error)


class TokenTests(unittest.TestCase):
    def test_every_errconnect_token(self) -> None:
        for token, (kind, reason, message) in ERRCONNECT.items():
            outcome = classify_rdp_exit(131, [err(token)])
            if token == "ERRCONNECT_CONNECT_UNDEFINED":
                continue
            self.assertEqual((outcome.kind, outcome.technical), (kind, token), token)

    def test_every_errinfo_token(self) -> None:
        for token, (kind, reason, _message) in ERRINFO.items():
            line = f"[INFO][com.freerdp.core] - [rdp_print_errinfo]: {token} (0x00000001):text"
            self.assertEqual(classify_rdp_exit(0, [line], connected=True).reason, reason, token)

    def test_only_wrong_credentials_retry_the_password(self) -> None:
        retry = {token for token, (kind, _r, _m) in ERRCONNECT.items() if kind is Kind.AUTH_FAILED}
        self.assertEqual(retry, {"ERRCONNECT_LOGON_FAILURE", "ERRCONNECT_WRONG_PASSWORD",
                                 "ERRCONNECT_AUTHENTICATION_FAILED", "ERRCONNECT_NO_OR_MISSING_CREDENTIALS"})
        for token in ("ERRCONNECT_ACCOUNT_LOCKED_OUT", "ERRCONNECT_ACCOUNT_DISABLED", "ERRCONNECT_ACCOUNT_EXPIRED",
                      "ERRCONNECT_PASSWORD_EXPIRED", "ERRCONNECT_PASSWORD_MUST_CHANGE",
                      "ERRCONNECT_LOGON_TYPE_NOT_GRANTED", "ERRCONNECT_DNS_NAME_NOT_FOUND",
                      "ERRCONNECT_TLS_CONNECT_FAILED"):
            self.assertFalse(classify_rdp_exit(134, [err(token)]).retry_password, token)

    def test_first_specific_error_wins_and_quirks_are_ignored(self) -> None:
        lines = ["[ERROR][com.freerdp.client.common.cmdline] - [parse_tls_cipher_options]: Command line parsing "
                 "failed at 'tls' value 'seclevel:0' [0]",
                 err("ERRCONNECT_LOGON_FAILURE"), err("ERRCONNECT_CONNECT_TRANSPORT_FAILED")]
        self.assertEqual(classify_rdp_exit(147, lines).kind, Kind.AUTH_FAILED)

    def test_certificate_rejection(self) -> None:
        for line in ("[ERROR][com.freerdp.crypto] - [tls_verify_certificate]: certificate not trusted, aborting.",
                     "[ERROR][com.freerdp.crypto] - @    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @"):
            outcome = classify_rdp_exit(143, [line, err("ERRCONNECT_TLS_CONNECT_FAILED")])
            self.assertEqual(outcome.kind, Kind.CERT_REJECTED)
            self.assertFalse(outcome.retry_password)

    def test_connected_line(self) -> None:
        self.assertTrue(connected_line("[INFO][com.freerdp.gdi] - [gdi_init_ex]: Local framebuffer format  "
                                       "PIXEL_FORMAT_BGRX32"))
        self.assertFalse(connected_line("[INFO][com.freerdp.gdi] - Remote framebuffer format PIXEL_FORMAT_RGB16"))


class TrustDecisionTests(unittest.TestCase):
    def cert(self, sha=A):
        return CertificateInfo("CN=win-01", "CN=win-01", "", "", sha)

    def test_table(self) -> None:
        cases = [
            ("ask", [], Action.ASK), ("ask", [A], Action.CONNECT), ("ask", [B], Action.CHANGED),
            ("tofu", [], Action.REMEMBER), ("tofu", [A], Action.CONNECT), ("tofu", [B], Action.CHANGED),
            ("pinned", [], Action.ASK), ("pinned", [A], Action.CONNECT), ("pinned", [B], Action.CHANGED),
            ("deny", [], Action.REFUSE), ("deny", [A], Action.CONNECT), ("deny", [B], Action.REFUSE),
            ("ignore", [B], Action.CONFIRM_IGNORE),
        ]
        for policy, pins, expected in cases:
            self.assertEqual(decide(policy, self.cert(), pins).action, expected, (policy, pins))

    def test_changed_reports_the_expected_pin(self) -> None:
        decision = decide("ask", self.cert(), ["bb" * 32])
        self.assertEqual((decision.fingerprint, decision.expected), (A, B))

    def test_no_tls_and_unreadable(self) -> None:
        self.assertEqual(decide("ask", None, [], security="rdp").action, Action.NO_TLS)
        self.assertEqual(decide("ask", None, []).action, Action.UNREADABLE)
        self.assertEqual(decide("ask", CertificateInfo(), []).action, Action.UNREADABLE)
        self.assertEqual(decide("ignore", None, [], security="rdp").action, Action.CONFIRM_IGNORE)


if __name__ == "__main__":
    unittest.main()
