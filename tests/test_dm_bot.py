"""Pure-Python tests for the DM command surface (dm_bot.py).

Covers:
- DMRateLimiter capacity, sliding window, per-pubkey isolation
- parse_command_token strip + lowercase + trailing-punct handling
- build_help_reply / build_stats_reply content + length
- dispatch_command routing including unknown
"""

import unittest

import dm_bot


class DMRateLimiterTest(unittest.TestCase):
    def test_per_hour_cap(self) -> None:
        rl = dm_bot.DMRateLimiter(per_hour=3)
        t = 1000.0
        self.assertTrue(rl.try_consume("alice", t))
        self.assertTrue(rl.try_consume("alice", t + 1))
        self.assertTrue(rl.try_consume("alice", t + 2))
        self.assertFalse(rl.try_consume("alice", t + 3))

    def test_window_slides(self) -> None:
        rl = dm_bot.DMRateLimiter(per_hour=2)
        t = 1000.0
        self.assertTrue(rl.try_consume("alice", t))
        self.assertTrue(rl.try_consume("alice", t + 100))
        self.assertFalse(rl.try_consume("alice", t + 200))
        # First send expires at t+3600; just past it, budget reopens.
        self.assertTrue(rl.try_consume("alice", t + 3601))
        # But the t+100 send still counts.
        self.assertFalse(rl.try_consume("alice", t + 3602))

    def test_per_pubkey_isolation(self) -> None:
        rl = dm_bot.DMRateLimiter(per_hour=1)
        t = 1000.0
        self.assertTrue(rl.try_consume("alice", t))
        self.assertFalse(rl.try_consume("alice", t + 1))
        # bob's budget is untouched by alice spending hers.
        self.assertTrue(rl.try_consume("bob", t + 1))

    def test_remaining_does_not_consume(self) -> None:
        rl = dm_bot.DMRateLimiter(per_hour=5)
        t = 1000.0
        rl.try_consume("alice", t)
        rl.try_consume("alice", t + 1)
        self.assertEqual(rl.remaining("alice", t + 1), 3)
        # And remaining() didn't spend a token.
        self.assertEqual(rl.remaining("alice", t + 1), 3)

    def test_rejects_zero_capacity(self) -> None:
        with self.assertRaises(ValueError):
            dm_bot.DMRateLimiter(per_hour=0)
        with self.assertRaises(ValueError):
            dm_bot.DMRateLimiter(per_hour=-1)


class ParseCommandTokenTest(unittest.TestCase):
    def test_lowercases(self) -> None:
        self.assertEqual(dm_bot.parse_command_token("HELP"), "help")
        self.assertEqual(dm_bot.parse_command_token("Stats"), "stats")

    def test_strips_whitespace(self) -> None:
        self.assertEqual(dm_bot.parse_command_token("   help   "), "help")
        self.assertEqual(dm_bot.parse_command_token("\tstats\n"), "stats")

    def test_takes_first_token_only(self) -> None:
        self.assertEqual(dm_bot.parse_command_token("stats please"), "stats")
        self.assertEqual(dm_bot.parse_command_token("help me out"), "help")

    def test_strips_trailing_punct(self) -> None:
        self.assertEqual(dm_bot.parse_command_token("stats?"), "stats")
        self.assertEqual(dm_bot.parse_command_token("help."), "help")
        self.assertEqual(dm_bot.parse_command_token("stats!"), "stats")

    def test_empty_and_whitespace(self) -> None:
        self.assertEqual(dm_bot.parse_command_token(""), "")
        self.assertEqual(dm_bot.parse_command_token("   "), "")


class BuildHelpReplyTest(unittest.TestCase):
    def test_contains_site_name_and_commands(self) -> None:
        reply = dm_bot.build_help_reply("TestNode")
        self.assertIn("TestNode", reply)
        self.assertIn("help", reply)
        self.assertIn("stats", reply)

    def test_short_enough_for_one_packet(self) -> None:
        # MeshCore packets are ~184 bytes; help reply should fit easily.
        reply = dm_bot.build_help_reply("CivicMesh-TestSite")
        self.assertLess(len(reply), 180)


_STATS = {
    "uptime_s": 12 * 3600 + 32 * 60,
    "cpu_temp_c": 51.3,
    "load_1m": 0.82,
    "msgs_sent": {"1h": 5, "24h": 42, "7d": 280},
    "wifi_sessions": {"1h": 2, "24h": 8, "7d": 50},
}


class BuildStatsReplyTest(unittest.TestCase):
    def test_includes_uptime_cpu_load(self) -> None:
        reply = dm_bot.build_stats_reply(
            site_name="TestNode", stats=_STATS,
            dm_remaining=3, dm_per_hour=6,
        )
        self.assertIn("12h32m", reply)
        self.assertIn("51c", reply)
        self.assertIn("0.82", reply)

    def test_includes_msg_and_session_windows(self) -> None:
        reply = dm_bot.build_stats_reply(
            site_name="TestNode", stats=_STATS,
            dm_remaining=3, dm_per_hour=6,
        )
        self.assertIn("msg 1h:5 24h:42 7d:280", reply)
        self.assertIn("sess 1h:2 24h:8 7d:50", reply)

    def test_includes_per_user_quota(self) -> None:
        reply = dm_bot.build_stats_reply(
            site_name="TestNode", stats=_STATS,
            dm_remaining=4, dm_per_hour=6,
        )
        self.assertIn("4/6", reply)
        self.assertIn("dms/hr", reply)

    def test_handles_missing_telemetry_gracefully(self) -> None:
        empty_stats = {
            "uptime_s": None, "cpu_temp_c": None, "load_1m": None,
            "msgs_sent": {"1h": 0, "24h": 0, "7d": 0},
            "wifi_sessions": {"1h": 0, "24h": 0, "7d": 0},
        }
        reply = dm_bot.build_stats_reply(
            site_name="TestNode", stats=empty_stats,
            dm_remaining=6, dm_per_hour=6,
        )
        # Should not raise; missing values render as '?'.
        self.assertIn("up ?", reply)
        self.assertIn("cpu ?", reply)
        self.assertIn("load ?", reply)

    def test_uptime_days_format(self) -> None:
        stats = dict(_STATS, uptime_s=3 * 86400 + 5 * 3600)
        reply = dm_bot.build_stats_reply(
            site_name="X", stats=stats, dm_remaining=1, dm_per_hour=6,
        )
        self.assertIn("3d5h", reply)

    def test_compact_overall_length(self) -> None:
        reply = dm_bot.build_stats_reply(
            site_name="CivicMesh-TestSite",
            stats=_STATS, dm_remaining=3, dm_per_hour=6,
        )
        # Target ~150 chars; relaxed cap at 200 keeps multi-packet
        # cost predictable on the LoRa side.
        self.assertLess(len(reply), 200)


class DispatchCommandTest(unittest.TestCase):
    def _ctx(self) -> dict:
        return {
            "site_name": "TestNode",
            "stats": _STATS,
            "dm_remaining": 3,
            "dm_per_hour": 6,
        }

    def test_help_routes_to_help_reply(self) -> None:
        reply = dm_bot.dispatch_command("help", self._ctx())
        self.assertIn("CivicMesh @ TestNode", reply)
        self.assertIn("stats — node", reply)

    def test_stats_routes_to_stats_reply(self) -> None:
        reply = dm_bot.dispatch_command("stats", self._ctx())
        self.assertIn("up 12h32m", reply)
        self.assertIn("3/6", reply)

    def test_unknown_returns_unknown(self) -> None:
        self.assertEqual(
            dm_bot.dispatch_command("nope", self._ctx()),
            "unknown command. send 'help' for commands.",
        )

    def test_empty_string_is_unknown(self) -> None:
        self.assertEqual(
            dm_bot.dispatch_command("", self._ctx()),
            "unknown command. send 'help' for commands.",
        )


if __name__ == "__main__":
    unittest.main()
