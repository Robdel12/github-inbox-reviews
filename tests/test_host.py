import json
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


HOST = Path(__file__).resolve().parents[1] / "host.py"


def review(state, oid="reviewed"):
    return {"id": "review", "state": state, "commit": {"oid": oid} if oid else None}


def pull_request(decision=None, comment=None, head="reviewed", requested=False, dismissals=()):
    return {
        "decision": {"nodes": [decision] if decision else []},
        "comment": {"nodes": [comment] if comment else []},
        "headRefOid": head,
        "viewerLatestReviewRequest": {"id": "request"} if requested else None,
        "timelineItems": {"nodes": list(dismissals)},
    }


class NativeHostTest(unittest.TestCase):
    def request(self, response, pulls, viewer="Octocat"):
        with tempfile.TemporaryDirectory() as directory:
            gh = Path(directory) / "gh"
            gh.write_text(
                f"#!{sys.executable}\n"
                "import json, sys\n"
                "assert sys.argv[1:] == ['api', '--hostname', 'github.com', 'graphql', '--input', '-']\n"
                f"assert json.load(sys.stdin)['variables']['login'] == {viewer!r}\n"
                f"print({json.dumps(response)!r})\n"
                f"sys.exit({int('errors' in response)})\n"
            )
            gh.chmod(0o700)
            message = json.dumps({"viewer": viewer, "pullRequests": pulls}).encode()
            result = subprocess.run(
                [sys.executable, HOST, "--serve", gh],
                input=struct.pack("=I", len(message)) + message,
                capture_output=True,
                check=True,
            )
        size, = struct.unpack("=I", result.stdout[:4])
        self.assertEqual(size, len(result.stdout[4:]))
        return json.loads(result.stdout[4:])

    def test_previous_reviews_survive_new_commits_and_later_comments(self):
        response = {
            "data": {
                "viewer": {"login": "octocat"},
                "r0": {
                    "p0": pull_request(review("APPROVED"), review("COMMENTED", "new"), head="new"),
                    "p1": pull_request(review("CHANGES_REQUESTED"), head="new", requested=True),
                    "p2": pull_request(comment=review("COMMENTED")),
                    "p3": pull_request(),
                    "p4": pull_request(
                        review("DISMISSED", None),
                        head="new",
                        dismissals=[{"review": {"id": "review"}, "previousReviewState": "APPROVED"}],
                    ),
                },
            }
        }
        pulls = [f"octo/widgets/{i}" for i in range(1, 6)]
        results = self.request(response, pulls)["reviews"]
        self.assertEqual(results[pulls[0]], {
            "state": "APPROVED", "head": "changed", "dismissed": False, "requestedAgain": False,
        })
        self.assertEqual(results[pulls[1]], {
            "state": "CHANGES_REQUESTED", "head": "changed", "dismissed": False, "requestedAgain": True,
        })
        self.assertEqual(results[pulls[2]], {
            "state": "COMMENTED", "head": "same", "dismissed": False, "requestedAgain": False,
        })
        self.assertEqual(results[pulls[3]], {"state": "UNREVIEWED"})
        self.assertEqual(results[pulls[4]], {
            "state": "APPROVED", "head": "unknown", "dismissed": True, "requestedAgain": False,
        })

    def test_inaccessible_repositories_keep_other_results_visible(self):
        response = {
            "data": {"viewer": {"login": "Octocat"}, "r0": None, "r1": {"p0": pull_request()}},
            "errors": [{"path": ["r0"]}],
        }
        self.assertEqual(self.request(response, ["octo/private/1", "octo/public/1"]), {
            "reviews": {"octo/private/1": {"state": "UNAVAILABLE"}, "octo/public/1": {"state": "UNREVIEWED"}},
        })

    def test_a_different_cli_account_does_not_supply_review_badges(self):
        result = self.request({"data": {"viewer": {"login": "Different"}}}, ["octo/widgets/1"])
        self.assertIn("accounts differ", result["error"])

    def test_invalid_repository_path_is_rejected(self):
        result = self.request({}, ['octo/widgets"/1'])
        self.assertEqual(result, {"error": "Invalid review lookup."})


if __name__ == "__main__":
    unittest.main()
