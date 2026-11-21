#!/usr/bin/env python3
"""Local end-to-end check against the running Compose stack (requires Playwright)."""
import argparse
import json
import secrets
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extras", action="store_true", help="Enable pilot, prompt logs and chaos on the isolated test board")
    parser.add_argument("--url", default="http://localhost:8081")
    parser.add_argument("--env-file", default="leaderboard/.env")
    parser.add_argument("--artifacts", default="artifacts")
    parser.add_argument("--image-id", required=True, help="Preloaded local baseline sha256 ID approved by the test organizer")
    args = parser.parse_args()
    from urllib.parse import urlsplit
    if urlsplit(args.url).hostname not in {"localhost", "127.0.0.1", "::1"}:
        parser.error("Mutating E2E is limited to a separate local test board")
    env = dict(line.split("=", 1) for line in Path(args.env_file).read_text().splitlines() if "=" in line and not line.startswith("#"))
    output = Path(args.artifacts)
    output.mkdir(parents=True, exist_ok=True)
    suffix = secrets.token_hex(3)
    nickname = "Local baseline " + suffix
    rejected_name = "Local rejected " + suffix
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(locale="pl-PL", viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(args.url + "/submit")
        page.wait_for_load_state("networkidle")
        expect(page.get_by_role("heading", name="Dołącz do gry")).to_be_visible()
        admin_context = browser.new_context(locale="pl-PL", http_credentials={"username": "admin", "password": env["ADMIN_PASSWORD"]})
        admin = admin_context.new_page()
        if args.extras:
            admin.goto(args.url + "/admin")
            for name in ("ai_pilot", "prompt_logs", "chaos"):
                admin.locator(f'input[name="{name}"]').check()
            admin.locator('form[action="/admin/extras"] button').click()

        def wait_for_publication(previous):
            deadline = time.monotonic() + 900
            started = False
            while time.monotonic() < deadline:
                data = page.request.get(args.url + "/api/leaderboard").json()
                if data["last_update"] != previous:
                    return data
                started = started or bool(data["running"] or data["requested"])
                if started and not data["running"] and not data["requested"]:
                    raise AssertionError("Batch ended without publication; inspect scheduler infrastructure errors")
                time.sleep(1)
            raise AssertionError("Batch did not finish")

        def invitation():
            admin.goto(args.url + "/admin")
            if args.extras:
                admin.locator('input[name="pilot"]').check()
            admin.get_by_role("button", name="Wydaj jednorazowe zaproszenie").click()
            return admin.locator(".invite-code").inner_text()

        def submit(name, source, code="", invite=""):
            page.goto(args.url + "/submit")
            page.get_by_label("Nick lub nazwa zespołu").fill(name)
            page.get_by_label("Obraz Docker lub adres repozytorium").fill(source)
            page.get_by_label("Kod zespołu", exact=False).fill(code)
            page.get_by_label("Zaproszenie od organizatora", exact=False).fill(invite)
            page.get_by_role("button", name="Zgłoś rozwiązanie").click()

        submit(nickname, "vibezone-starter:local", invite=invitation())
        expect(page.get_by_role("heading", name="Zgłoszenie zapisane")).to_be_visible()
        code = page.locator(".team-code").inner_text()
        submit(nickname, "vibezone-starter:local", "wrong-code")
        expect(page.get_by_role("alert")).to_contain_text("Ta nazwa jest zajęta")
        submit(nickname, "vibezone-starter:local", code)
        expect(page.get_by_role("heading", name="Zgłoszenie zapisane")).to_be_visible()
        submit(rejected_name, "vibezone-missing:" + suffix, invite=invitation())
        page.goto(args.url + "/submit")
        page.wait_for_load_state("networkidle")
        print("Form creation, ownership and update passed", flush=True)

        admin.goto(args.url + "/admin")
        expect(admin.get_by_role("heading", name="Panel organizatora")).to_be_visible()
        data = page.request.get(args.url + "/api/leaderboard").json()
        submission_id = next(row["id"] for row in data["submissions"] if row["nickname"] == nickname and row["current"])
        form = admin.locator(f'form[action="/admin/submissions/{submission_id}/approve"]')
        form.get_by_label("Lokalne ID sprawdzonego obrazu").fill(args.image_id)
        form.get_by_role("button", name="Zatwierdź obraz").click()
        before = page.request.get(args.url + "/api/leaderboard").json()["last_update"]
        admin.get_by_role("button", name="Uruchom batch").click()
        page.goto(args.url + "/leaderboard")
        data = wait_for_publication(before)
        expect(page.locator("#ranking-rows")).to_contain_text(nickname, timeout=15000)
        assert data["last_update"] != before
        result = next(row["result"] for row in data["ranking"] if row["nickname"] == nickname)
        assert result["status"] == "passed" and result["profile"]["name"] == "smoke"
        assert any(row["nickname"] == rejected_name for row in data["rejected"])
        assert code not in json.dumps(data)
        if args.extras:
            row = next(row for row in data["ranking"] if row["nickname"] == nickname)
            assert row["extras"]["hidden"]["status"] == "passed"
            chaos_status = row["extras"]["chaos"]["status"]
            assert chaos_status in {"passed", "failed"}
            assert ("survivor" in row["badges"]) == (chaos_status == "passed")
            assert "pilot" in row["badges"]
            assert any(entry["nickname"] == nickname for entry in data["extras"]["pilot"])
        print("Docker batch, rejection and live refresh passed", flush=True)

        previous = data["last_update"]
        previous_run = result["id"]
        admin.get_by_role("button", name="Uruchom batch").click()
        data = wait_for_publication(previous)
        result = next(row["result"] for row in data["ranking"] if row["nickname"] == nickname)
        # Identical images may share a result within a batch; its run ID must change across batches.
        assert result["id"] != previous_run
        assert len(result["repetitions"]) == 3
        rejected_id = next(row["id"] for row in data["submissions"] if row["nickname"] == rejected_name)
        admin.reload()
        admin.get_by_role("button", name=f"Ukryj zgłoszenie {rejected_id}", exact=True).click()
        data = page.request.get(args.url + "/api/leaderboard").json()
        assert not any(row["nickname"] == rejected_name for row in data["submissions"] + data["rejected"])
        print("Remeasurement and admin hide passed", flush=True)

        page.reload()
        page.wait_for_load_state("networkidle")
        page.screenshot(path=str(output / "leaderboard-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_load_state("networkidle")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile page overflows"
        expect(page.locator("#ranking-rows")).to_be_visible()
        page.screenshot(path=str(output / "leaderboard-mobile.png"), full_page=True)
        page.set_viewport_size({"width": 1920, "height": 1080})
        page.goto(args.url + "/screen")
        page.wait_for_load_state("networkidle")
        assert page.locator("form").count() == 0
        expect(page.locator("#ranking-rows")).to_contain_text(nickname)
        page.screenshot(path=str(output / "leaderboard-screen.png"), full_page=True)
        page.goto(args.url + "/submit?lang=en")
        expect(page.get_by_role("heading", name="Join the challenge")).to_be_visible()
        expect(page.locator(".test-banner")).to_contain_text("Test results")
        page.goto(args.url + "/leaderboard?lang=en")
        with page.expect_response("**/api/leaderboard?lang=en", timeout=15000):
            pass
        expect(page.locator("#refresh-status")).to_have_text("Refreshes every 10 seconds")
        page.goto(args.url + "/screen")
        expect(page.locator("html")).to_have_attribute("lang", "en")
        expect(page.locator("#ranking-rows")).to_contain_text("test results")
        page.get_by_role("link", name="PL", exact=True).click()
        expect(page.locator("html")).to_have_attribute("lang", "pl")
        assert errors == [], errors
        report = {"passed": True, "nickname": nickname, "result": result, "browser_errors": errors,
                  "checks": ["submit", "ownership", "update", "admin", "docker batch", "rejection", "auto refresh",
                             "invitation", "approval", "remeasurement", "hide", "mobile width", "screen view", "PL/EN switch", "English live refresh"]}
        (output / "ui-check.json").write_text(json.dumps(report, indent=2))
        browser.close()
    print("UI check passed; screenshots and report in " + str(output))


if __name__ == "__main__":
    main()
