"""End-to-end test for the agent-relay decision hub.

Covers: register 2 agents -> create thread with initial prompt + invite ->
both join with different mandates -> thread messages -> proposal -> votes ->
thread decided with correct outcome -> mandate privacy -> reject kills a
proposal -> creator manual decide -> decided threads reject joins.

Run:  python3 tests/test_e2e.py
"""
import os
import sys
import tempfile

# Fresh throwaway DB before app import (import runs create_all).
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient  # noqa: E402

import app  # noqa: E402

client = TestClient(app.app)
PASS = []


def check(name, cond, extra=""):
    assert cond, f"FAIL: {name} {extra}"
    PASS.append(name)
    print(f"  ok: {name}")


def H(secret):
    return {"X-Agent-Secret": secret}


print("== register ==")
r = client.post("/v1/agents/register", json={"handle": "alice"})
check("register alice", r.status_code == 201, r.text)
alice = r.json()["secret"]
r = client.post("/v1/agents/register", json={"handle": "bob"})
check("register bob", r.status_code == 201, r.text)
bob = r.json()["secret"]
r = client.post("/v1/agents/register", json={"handle": "alice"})
check("duplicate handle 409", r.status_code == 409)

print("== create thread ==")
r = client.post("/v1/threads", headers=H(alice), json={
    "title": "Pick a launch date",
    "initial_prompt": "Decide the launch date for our joint skill. Must be a weekday.",
    "invite": ["bob"],
    "mandate": "Alice's private mandate: prefer Friday; never agree to Monday.",
})
check("create thread 201", r.status_code == 201, r.text)
tid = r.json()["thread_id"]
check("invite recorded", r.json()["invited"] == ["bob"], r.text)

print("== list threads ==")
r = client.get("/v1/threads")
check("list has thread", any(t["id"] == tid for t in r.json()["threads"]))
r = client.get("/v1/threads", params={"status": "open"})
check("status=open filter", any(t["id"] == tid for t in r.json()["threads"]))

print("== join ==")
r = client.post(f"/v1/threads/{tid}/join", headers=H(bob), json={
    "mandate": "Bob's private mandate: any weekday is fine, push for Wednesday.",
})
check("bob joins", r.status_code == 200, r.text)
r = client.post(f"/v1/threads/{tid}/join", headers=H(bob), json={"mandate": "x"})
check("double join 409", r.status_code == 409)
r = client.post("/v1/threads/doesnotexist/join", headers=H(bob), json={"mandate": "x"})
check("join missing thread 404", r.status_code == 404)

print("== thread messages ==")
r = client.post("/v1/messages", headers=H(alice),
                json={"thread_id": tid, "text": "I propose we aim for Friday."})
check("alice thread msg", r.status_code == 201 and r.json()["thread_id"] == tid, r.text)
r = client.post("/v1/messages", headers=H(bob),
                json={"thread_id": tid, "text": "Wednesday works better for my human."})
check("bob thread msg", r.status_code == 201, r.text)
r = client.get("/v1/messages", headers=H(alice), params={"thread_id": tid})
msgs = r.json()["messages"]
check("both msgs visible to alice", [m["text"] for m in msgs] ==
      ["I propose we aim for Friday.", "Wednesday works better for my human."], r.text)
r = client.get("/v1/messages", headers=H(bob), params={"thread_id": tid})
check("both msgs visible to bob", len(r.json()["messages"]) == 2)
# thread messages don't leak into the DM inbox
r = client.get("/v1/messages", headers=H(alice))
check("DM inbox unaffected", r.json()["messages"] == [])

print("== direct messages still work ==")
r = client.post("/v1/messages", headers=H(alice), json={"to": "bob", "text": "hey"})
check("DM send", r.status_code == 201, r.text)
r = client.get("/v1/messages", headers=H(bob))
check("DM inbox", [m["text"] for m in r.json()["messages"]] == ["hey"], r.text)
r = client.post("/v1/messages/ack", headers=H(bob),
                json={"ids": [r.json()["messages"][0]["id"]]})
check("DM ack", r.json()["acked"] == 1)

print("== proposal + unanimous vote ==")
r = client.post(f"/v1/threads/{tid}/proposals", headers=H(alice),
                json={"text": "We launch on Friday."})
check("proposal created", r.status_code == 201, r.text)
pid = r.json()["proposal_id"]
check("proposer auto-accept", r.json()["accepts"] == 1, r.text)
r = client.post(f"/v1/threads/{tid}/proposals/{pid}/vote", headers=H(bob),
                json={"vote": "accept"})
check("bob accepts", r.status_code == 200, r.text)
check("proposal accepted", r.json()["status"] == "accepted", r.text)
check("thread decided", r.json()["thread_status"] == "decided")
check("outcome = proposal text", r.json()["outcome"] == "We launch on Friday.")
r = client.get(f"/v1/threads/{tid}")
d = r.json()
check("detail outcome", d["outcome"] == "We launch on Friday." and d["status"] == "decided")
check("detail decided_at set", d["decided_at"] is not None)

print("== mandate privacy ==")
blob = r.text
check("no mandate text in public detail",
      "Alice's private mandate" not in blob and "Bob's private mandate" not in blob)
check("no my_mandate for anonymous", "my_mandate" not in d)
r = client.get(f"/v1/threads/{tid}", headers=H(bob))
check("bob sees own mandate only",
      r.json().get("my_mandate") == "Bob's private mandate: any weekday is fine, push for Wednesday."
      and "Alice's private mandate" not in r.text)
r = client.get(f"/v1/threads/{tid}", headers=H(alice))
check("alice sees own mandate only",
      r.json().get("my_mandate") == "Alice's private mandate: prefer Friday; never agree to Monday."
      and "Bob's private mandate" not in r.text)

print("== decided thread is frozen ==")
r = client.post(f"/v1/threads/{tid}/proposals", headers=H(alice), json={"text": "late"})
check("no proposals on decided thread (410)", r.status_code == 410, r.text)
r = client.post("/v1/messages", headers=H(alice),
                json={"thread_id": tid, "text": "late msg"})
check("no messages on decided thread (410)", r.status_code == 410, r.text)

print("== reject kills a proposal ==")
r = client.post("/v1/threads", headers=H(alice), json={
    "title": "Second thread", "initial_prompt": "Pick a color.", "invite": ["bob"]})
t2 = r.json()["thread_id"]
client.post(f"/v1/threads/{t2}/join", headers=H(bob), json={"mandate": "bob m2"})
r = client.post(f"/v1/threads/{t2}/proposals", headers=H(alice), json={"text": "Blue."})
p2 = r.json()["proposal_id"]
r = client.post(f"/v1/threads/{t2}/proposals/{p2}/vote", headers=H(bob),
                json={"vote": "reject"})
check("reject -> rejected", r.json()["status"] == "rejected", r.text)
check("thread still open", r.json()["thread_status"] == "open")
r = client.post(f"/v1/threads/{t2}/proposals/{p2}/vote", headers=H(bob),
                json={"vote": "accept"})
check("vote on rejected proposal 409", r.status_code == 409)
r = client.post(f"/v1/threads/{t2}/proposals", headers=H(bob), json={"text": "Red."})
p3 = r.json()["proposal_id"]
r = client.post(f"/v1/threads/{t2}/proposals/{p3}/vote", headers=H(alice),
                json={"vote": "accept"})
check("second proposal accepted", r.json()["status"] == "accepted")
check("thread decided on red", r.json()["outcome"] == "Red.")

print("== creator manual decide ==")
r = client.post("/v1/threads", headers=H(alice), json={
    "title": "Third thread", "initial_prompt": "Pick a venue.", "invite": ["bob"],
    "mandate": "alice m3"})
t3 = r.json()["thread_id"]
client.post(f"/v1/threads/{t3}/join", headers=H(bob), json={"mandate": "bob m3"})
client.post("/v1/messages", headers=H(alice),
            json={"thread_id": t3, "text": "How about the park?"})
client.post("/v1/messages", headers=H(bob),
            json={"thread_id": t3, "text": "Park works."})
r = client.post(f"/v1/threads/{t3}/decide", headers=H(bob),
                json={"outcome": "The park."})
check("non-creator decide 403", r.status_code == 403, r.text)
r = client.post(f"/v1/threads/{t3}/decide", headers=H(alice),
                json={"outcome": "The park, Saturday noon."})
check("creator decide", r.status_code == 200, r.text)
check("manual outcome", r.json()["outcome"] == "The park, Saturday noon.")
r = client.get(f"/v1/threads/{t3}")
check("detail shows manual decision",
      r.json()["status"] == "decided" and r.json()["outcome"] == "The park, Saturday noon.")

print("== join decided thread ==")
r = client.post("/v1/agents/register", json={"handle": "carol"})
carol = r.json()["secret"]
r = client.post(f"/v1/threads/{t3}/join", headers=H(carol), json={"mandate": "late"})
check("join decided thread 410", r.status_code == 410, r.text)

print("== non-participant cannot read thread ==")
r = client.get("/v1/messages", headers=H(carol), params={"thread_id": t2})
check("non-participant thread read 403", r.status_code == 403, r.text)

print("== UI smoke ==")
r = client.get("/")
check("landing HTML", r.status_code == 200 and "<form" in r.text and "Step 0" in r.text)
check("landing lists threads", "Pick a launch date" in r.text)
check("landing leaks no mandates", "Alice's private mandate" not in r.text)
r = client.get(f"/t/{t2}")
check("thread page renders", r.status_code == 200 and "Pick a color" in r.text)
check("thread page shows initial prompt", "Pick a color." in r.text)
check("thread page leaks no mandates",
      "bob m2" not in r.text and "Alice's private mandate" not in r.text)
check("thread page shows decision", "Red." in r.text)
r = client.get("/v1/health")
check("health ok", r.json()["ok"] is True and r.json()["threads"] == 3, r.text)

print(f"\nALL {len(PASS)} CHECKS PASSED")
