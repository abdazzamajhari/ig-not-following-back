#!/usr/bin/env python3
"""
Pemeriksa akun Instagram yang tidak mengikuti balik.

Skrip ini mengambil data terkini dari daftar Followers dan Following yang
ditampilkan Instagram pada sesi browser Anda. Playwright dipakai untuk membuka
browser dan menggulir daftar dinamis; BeautifulSoup dipakai untuk mengekstrak
username dari HTML yang sedang tampil.

Instalasi:
    python -m pip install beautifulsoup4 playwright
    python -m playwright install chromium

Pemakaian:
    python instagram_not_following_back.py USERNAME_ANDA

Contoh:
    python instagram_not_following_back.py azzamcontoh --output hasil.csv

Catatan keamanan:
    - Login dilakukan sendiri pada jendela Chromium, termasuk 2FA bila aktif.
    - Skrip tidak meminta kata sandi dan tidak membaca atau mengekspor cookie.
    - Sesi browser disimpan di folder lokal ``instagram_browser_profile`` agar
      login tidak perlu diulang pada penggunaan berikutnya.
    - Gunakan hanya untuk akun Anda dan jangan membagikan folder sesi tersebut.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

try:
    from bs4 import BeautifulSoup
except ImportError as exc:  # pragma: no cover - pesan instalasi untuk pengguna
    raise SystemExit(
        "Dependensi belum terpasang. Jalankan: "
        "python -m pip install beautifulsoup4 playwright"
    ) from exc

try:
    from playwright.sync_api import (
        Error as PlaywrightError,
        Locator,
        Page,
        TimeoutError as PlaywrightTimeoutError,
        sync_playwright,
    )
except ImportError as exc:  # pragma: no cover - pesan instalasi untuk pengguna
    raise SystemExit(
        "Dependensi belum terpasang. Jalankan: "
        "python -m pip install beautifulsoup4 playwright"
    ) from exc


INSTAGRAM_URL = "https://www.instagram.com"
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._]{1,30}$")

# Segmen URL milik fitur Instagram, bukan username profil.
RESERVED_PATHS = {
    "about",
    "accounts",
    "ads",
    "api",
    "challenge",
    "developer",
    "direct",
    "directory",
    "emails",
    "explore",
    "legal",
    "oauth",
    "p",
    "privacy",
    "reel",
    "reels",
    "stories",
    "terms",
    "web",
}

RELATION_LABELS = {
    "followers": ("followers", "follower", "pengikut"),
    "following": ("following", "mengikuti", "diikuti"),
}


@dataclass(frozen=True)
class CollectionResult:
    relation: str
    usernames: set[str]
    expected_count: int | None
    rounds: int
    reached_end: bool

    @property
    def is_partial(self) -> bool:
        return (
            self.expected_count is not None
            and len(self.usernames) < self.expected_count
        )


def normalize_username(value: str) -> str | None:
    """Validasi dan normalkan username Instagram menjadi huruf kecil."""
    candidate = unquote(value).strip().strip("/@").lower()
    if not candidate or candidate in RESERVED_PATHS:
        return None
    if not USERNAME_PATTERN.fullmatch(candidate):
        return None
    return candidate


def username_from_href(href: str | None) -> str | None:
    """Ambil username dari tautan profil Instagram satu-segmen."""
    if not href:
        return None

    parsed = urlparse(href)
    if parsed.netloc:
        host = parsed.netloc.lower().split(":", 1)[0]
        if host not in {"instagram.com", "www.instagram.com"}:
            return None

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) == 2 and parts[0] == "_u":
        parts = parts[1:]
    if len(parts) != 1:
        return None
    return normalize_username(parts[0])


def usernames_from_html(html: str) -> set[str]:
    """Ekstrak username profil dari fragmen HTML menggunakan BeautifulSoup."""
    soup = BeautifulSoup(html, "html.parser")
    usernames: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        username = username_from_href(anchor.get("href"))
        if username:
            usernames.add(username)
    return usernames


def parse_count(text: str | None) -> int | None:
    """Ubah jumlah seperti 1,234, 1.234, 1.2K, atau 2 jt menjadi integer."""
    if not text:
        return None

    cleaned = text.strip().lower().replace("\u00a0", " ")
    match = re.search(
        r"(\d[\d.,\s]*)(?:\s*)(miliar|rb|jt|k|m|b)?",
        cleaned,
    )
    if not match:
        return None

    raw_number = re.sub(r"\s+", "", match.group(1))
    suffix = match.group(2)
    multipliers = {
        "k": 1_000,
        "rb": 1_000,
        "m": 1_000_000,
        "jt": 1_000_000,
        "b": 1_000_000_000,
        "miliar": 1_000_000_000,
    }

    if suffix:
        # Dalam bentuk ringkas, pemisah terakhir adalah desimal: 1,2K / 1.2K.
        normalized = raw_number.replace(",", ".")
        if normalized.count(".") > 1:
            pieces = normalized.split(".")
            normalized = "".join(pieces[:-1]) + "." + pieces[-1]
        try:
            return int(float(normalized) * multipliers[suffix])
        except ValueError:
            return None

    digits = re.sub(r"\D", "", raw_number)
    return int(digits) if digits else None


def relation_count(link: Locator) -> int | None:
    """Baca jumlah relationship dari title/aria-label/teks tautan profil."""
    candidates: list[str] = link.evaluate(
        """
        (el) => {
          const values = [el.getAttribute('title'), el.getAttribute('aria-label'), el.innerText];
          for (const node of el.querySelectorAll('[title], [aria-label]')) {
            values.push(node.getAttribute('title'));
            values.push(node.getAttribute('aria-label'));
            values.push(node.innerText);
          }
          return values.filter(Boolean);
        }
        """
    )
    for candidate in candidates:
        count = parse_count(candidate)
        if count is not None:
            return count
    return None


def is_login_page(page: Page) -> bool:
    """Deteksi formulir login tanpa mengakses nilai kolom atau cookie."""
    return (
        "/accounts/login" in page.url
        or "/challenge/" in page.url
        or page.locator('input[name="username"]').count() > 0
    )


def wait_for_navigation_to_settle(page: Page, max_wait_ms: int = 15_000) -> None:
    """Tunggu rangkaian redirect setelah login manual berhenti."""
    previous_url = ""
    stable_checks = 0
    checks = max(1, max_wait_ms // 750)

    for _ in range(checks):
        try:
            page.wait_for_load_state("domcontentloaded", timeout=2_000)
        except PlaywrightTimeoutError:
            pass

        current_url = page.url
        if current_url == previous_url:
            stable_checks += 1
        else:
            previous_url = current_url
            stable_checks = 0

        if stable_checks >= 3:
            return
        page.wait_for_timeout(750)


def goto_with_retry(
    page: Page,
    url: str,
    timeout_ms: int,
    retries: int = 4,
) -> None:
    """Buka URL dan ulangi bila redirect Instagram memotong navigasi."""
    interruption_markers = (
        "interrupted by another navigation",
        "net::err_aborted",
        "navigation to",
    )

    for attempt in range(1, retries + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            return
        except PlaywrightError as exc:
            message = str(exc).lower()
            interrupted = (
                "interrupted by another navigation" in message
                or "net::err_aborted" in message
                or (
                    interruption_markers[2] in message
                    and "is interrupted" in message
                )
            )
            if not interrupted:
                raise
            if attempt == retries:
                raise RuntimeError(
                    "Navigasi Instagram terus terinterupsi oleh pengalihan otomatis. "
                    "Tunggu hingga beranda benar-benar diam, lalu jalankan ulang."
                ) from exc

            wait_for_navigation_to_settle(page, max_wait_ms=6_000)
            page.wait_for_timeout(1_000 * attempt)


def ensure_authenticated(page: Page, profile_username: str, timeout_ms: int) -> None:
    """Pastikan pengguna sudah login melalui interaksi manual di browser."""
    # Profil publik tetap dapat dibuka tanpa login, tetapi Instagram biasanya
    # tidak memberikan daftar relationship lengkap. Halaman pengaturan dipakai
    # hanya sebagai pemeriksaan sesi; skrip tidak membaca atau mengubah isinya.
    session_check_url = f"{INSTAGRAM_URL}/accounts/edit/"
    goto_with_retry(page, session_check_url, timeout_ms)
    page.wait_for_timeout(2_000)

    if is_login_page(page):
        print("\nLOGIN DIPERLUKAN")
        goto_with_retry(
            page,
            f"{INSTAGRAM_URL}/accounts/login/",
            timeout_ms,
        )
        print("1. Masuk secara manual pada jendela Chromium yang terbuka.")
        print("2. Selesaikan autentikasi dua faktor jika diminta.")
        input("3. Setelah beranda Instagram tampil, tekan ENTER di terminal ini... ")

        print("Menunggu pengalihan login Instagram selesai...")
        wait_for_navigation_to_settle(page)
        goto_with_retry(page, session_check_url, timeout_ms)
        page.wait_for_timeout(2_000)
        if is_login_page(page):
            raise RuntimeError(
                "Login belum berhasil. Selesaikan login/2FA pada jendela Chromium, "
                "kemudian jalankan ulang skrip."
            )

    profile_url = f"{INSTAGRAM_URL}/{profile_username}/"
    goto_with_retry(page, profile_url, timeout_ms)
    page.locator("main").first.wait_for(state="visible", timeout=timeout_ms)
    page.wait_for_timeout(2_500)

    if is_login_page(page):
        raise RuntimeError("Login belum berhasil atau sesi ditolak Instagram.")

    unavailable = page.get_by_text(
        re.compile(
            r"sorry, this page isn't available|maaf, halaman ini tidak tersedia",
            re.IGNORECASE,
        )
    )
    if unavailable.count() > 0:
        raise RuntimeError(
            f"Profil @{profile_username} tidak ditemukan atau tidak dapat diakses."
        )


def find_relation_link(
    page: Page,
    profile_username: str,
    relation: str,
    wait_ms: int = 12_000,
) -> Locator | None:
    """Temukan tautan/tombol relationship pada variasi UI Instagram."""
    selectors = [
        f'a[href="/{profile_username}/{relation}/"]',
        f'a[href="/{profile_username}/{relation}"]',
        f'a[href^="/{profile_username}/{relation}?"]',
        f'a[href$="/{relation}/"]',
        f'a[href$="/{relation}"]',
    ]
    labels = RELATION_LABELS[relation]
    deadline_steps = max(1, wait_ms // 500)

    for _ in range(deadline_steps):
        for selector in selectors:
            locator = page.locator(selector).first
            if locator.count() > 0 and locator.is_visible():
                return locator

        # UI tertentu merender penghitung sebagai button tanpa href. Batasi
        # pencarian pada area profil dan minta adanya angka agar tombol aksi
        # "Following" tidak keliru dipilih.
        candidates = page.locator("header a, header button, main a, main button")
        for index in range(candidates.count()):
            candidate = candidates.nth(index)
            if not candidate.is_visible():
                continue
            metadata: dict[str, str] = candidate.evaluate(
                """
                (el) => ({
                  text: el.innerText || '',
                  aria: el.getAttribute('aria-label') || '',
                  title: el.getAttribute('title') || '',
                  href: el.getAttribute('href') || ''
                })
                """
            )
            combined = " ".join(metadata.values()).lower()
            contains_label = any(label in combined for label in labels)
            contains_count = bool(re.search(r"\d", combined))
            contains_relation_url = f"/{relation}" in metadata["href"].lower()
            if contains_label and (contains_count or contains_relation_url):
                return candidate

        page.wait_for_timeout(500)

    return None


def open_relation_dialog(
    page: Page,
    profile_username: str,
    relation: str,
    timeout_ms: int = 15_000,
) -> tuple[Locator, int | None]:
    """Buka modal relationship melalui elemen profil atau URL langsung."""
    link = find_relation_link(page, profile_username, relation)
    expected_count = relation_count(link) if link is not None else None
    dialog = page.locator('div[role="dialog"]').last

    if link is not None:
        try:
            link.scroll_into_view_if_needed(timeout=timeout_ms)
            link.click(timeout=timeout_ms)
            dialog.wait_for(state="visible", timeout=timeout_ms)
            return dialog, expected_count
        except PlaywrightError:
            # Lanjutkan dengan rute langsung jika klik diintersepsi UI.
            pass

    direct_url = f"{INSTAGRAM_URL}/{profile_username}/{relation}/"
    goto_with_retry(page, direct_url, timeout_ms)
    page.wait_for_timeout(2_000)
    if is_login_page(page):
        raise RuntimeError(
            f"Sesi login berakhir ketika membuka {relation}. Jalankan ulang dan login lagi."
        )

    try:
        dialog.wait_for(state="visible", timeout=timeout_ms)
        return dialog, expected_count
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(
            f"Daftar {relation} tidak dapat dibuka, baik melalui elemen profil "
            "maupun URL langsung. Instagram mungkin sedang membatasi sesi ini."
        ) from exc


def dialog_snapshot(dialog: Locator, should_scroll: bool) -> dict[str, Any]:
    """Ambil HTML area gulir dan, bila diminta, gulirkan satu layar."""
    return dialog.evaluate(
        """
        (root, shouldScroll) => {
          const candidates = [root, ...root.querySelectorAll('*')]
            .filter((el) => el.scrollHeight > el.clientHeight + 8);
          candidates.sort((a, b) => {
            const aScrollable = getComputedStyle(a).overflowY === 'auto' ||
                                getComputedStyle(a).overflowY === 'scroll';
            const bScrollable = getComputedStyle(b).overflowY === 'auto' ||
                                getComputedStyle(b).overflowY === 'scroll';
            if (aScrollable !== bScrollable) return bScrollable - aScrollable;
            return b.scrollHeight - a.scrollHeight;
          });
          const scroller = candidates[0] || root;
          const before = scroller.scrollTop;
          if (shouldScroll) {
            scroller.scrollBy(0, Math.max(400, scroller.clientHeight * 0.85));
          }
          return {
            html: scroller.innerHTML,
            scrollTop: scroller.scrollTop,
            previousScrollTop: before,
            clientHeight: scroller.clientHeight,
            scrollHeight: scroller.scrollHeight,
            atBottom: scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 8
          };
        }
        """,
        should_scroll,
    )


def collect_relation(
    page: Page,
    profile_username: str,
    relation: str,
    delay_ms: int,
    max_rounds: int,
    stall_rounds: int,
    timeout_ms: int,
) -> CollectionResult:
    """Buka modal relationship dan kumpulkan seluruh username yang termuat."""
    dialog, expected_count = open_relation_dialog(
        page,
        profile_username,
        relation,
        timeout_ms=timeout_ms,
    )
    page.wait_for_timeout(1_200)

    usernames: set[str] = set()
    stalled = 0
    reached_end = False
    rounds_used = 0

    label = "pengikut" if relation == "followers" else "yang diikuti"
    expected_label = str(expected_count) if expected_count is not None else "tidak terbaca"
    print(f"\nMengambil {label}; jumlah pada profil: {expected_label}")

    try:
        for round_number in range(1, max_rounds + 1):
            rounds_used = round_number
            before_size = len(usernames)

            snapshot = dialog_snapshot(dialog, should_scroll=False)
            usernames.update(usernames_from_html(snapshot["html"]))

            if expected_count is not None and len(usernames) >= expected_count:
                reached_end = True
                break

            movement = dialog_snapshot(dialog, should_scroll=True)
            page.wait_for_timeout(delay_ms)

            after_snapshot = dialog_snapshot(dialog, should_scroll=False)
            usernames.update(usernames_from_html(after_snapshot["html"]))

            no_new_users = len(usernames) == before_size
            no_movement = (
                movement["scrollTop"] == movement["previousScrollTop"]
                or after_snapshot["atBottom"]
            )
            stalled = stalled + 1 if no_new_users and no_movement else 0

            if round_number == 1 or round_number % 10 == 0:
                print(f"  ditemukan {len(usernames)} akun...", flush=True)

            if stalled >= stall_rounds:
                reached_end = True
                break
    finally:
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)

    print(f"  selesai: {len(usernames)} akun unik")
    return CollectionResult(
        relation=relation,
        usernames=usernames,
        expected_count=expected_count,
        rounds=rounds_used,
        reached_end=reached_end,
    )


def write_csv(path: Path, usernames: set[str]) -> None:
    """Tulis hasil dengan UTF-8 BOM agar terbaca baik oleh Microsoft Excel."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["username", "profile_url"])
        writer.writeheader()
        for username in sorted(usernames):
            writer.writerow(
                {
                    "username": username,
                    "profile_url": f"{INSTAGRAM_URL}/{username}/",
                }
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Ambil Followers dan Following langsung dari sesi Instagram, lalu "
            "simpan akun yang tidak mengikuti balik ke CSV."
        )
    )
    parser.add_argument("username", help="Username profil Anda, tanpa karakter @")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("instagram_not_following_back.csv"),
        help="Lokasi CSV hasil (default: instagram_not_following_back.csv)",
    )
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=Path("instagram_browser_profile"),
        help="Folder sesi Chromium lokal (default: instagram_browser_profile)",
    )
    parser.add_argument(
        "--scroll-delay",
        type=float,
        default=0.9,
        help="Jeda antar-pengguliran dalam detik (default: 0.9)",
    )
    parser.add_argument(
        "--max-scroll-rounds",
        type=int,
        default=5_000,
        help="Batas putaran gulir per daftar (default: 5000)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Timeout pemuatan halaman dalam detik (default: 60)",
    )
    return parser


def validate_args(args: argparse.Namespace) -> str:
    username = normalize_username(args.username)
    if not username:
        raise ValueError("Username tidak valid. Gunakan huruf, angka, titik, atau garis bawah.")
    if args.scroll_delay < 0.5:
        raise ValueError("--scroll-delay minimal 0.5 detik untuk mengurangi beban/rate limit.")
    if args.max_scroll_rounds < 1:
        raise ValueError("--max-scroll-rounds harus minimal 1.")
    if args.timeout < 10:
        raise ValueError("--timeout harus minimal 10 detik.")
    return username


def run(args: argparse.Namespace) -> int:
    username = validate_args(args)
    output_path = args.output.resolve()
    profile_dir = args.profile_dir.resolve()
    delay_ms = int(args.scroll_delay * 1_000)

    print("Browser akan terbuka. Jangan menutupnya selama pengambilan data.")
    print(f"Folder sesi lokal: {profile_dir}")

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            viewport={"width": 1280, "height": 900},
            locale="id-ID",
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            ensure_authenticated(page, username, args.timeout * 1_000)

            followers = collect_relation(
                page,
                username,
                "followers",
                delay_ms,
                args.max_scroll_rounds,
                stall_rounds=8,
                timeout_ms=args.timeout * 1_000,
            )
            following = collect_relation(
                page,
                username,
                "following",
                delay_ms,
                args.max_scroll_rounds,
                stall_rounds=8,
                timeout_ms=args.timeout * 1_000,
            )
        finally:
            context.close()

    not_following_back = following.usernames - followers.usernames
    write_csv(output_path, not_following_back)

    print("\nRINGKASAN")
    print(f"Followers terbaca           : {len(followers.usernames)}")
    print(f"Following terbaca           : {len(following.usernames)}")
    print(f"Tidak mengikuti balik       : {len(not_following_back)}")
    print(f"CSV                          : {output_path}")

    partial_results = [result for result in (followers, following) if result.is_partial]
    if partial_results:
        for result in partial_results:
            print(
                "PERINGATAN: "
                f"{result.relation} hanya terbaca {len(result.usernames)} dari "
                f"perkiraan {result.expected_count}. CSV mungkin belum lengkap.",
                file=sys.stderr,
            )
        return 2

    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return run(args)
    except (ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except PlaywrightTimeoutError:
        print(
            "ERROR: Instagram tidak selesai dimuat. Periksa koneksi atau naikkan --timeout.",
            file=sys.stderr,
        )
        return 1
    except PlaywrightError as exc:
        message = str(exc)
        missing_browser = any(
            marker in message.lower()
            for marker in (
                "executable doesn't exist",
                "failed to launch browser",
                "playwright install",
            )
        )
        hint = (
            "\nPasang Chromium dengan: python -m playwright install chromium"
            if missing_browser
            else "\nKirimkan pesan ERROR lengkap ini jika masalah berulang."
        )
        print(f"ERROR Playwright: {message}{hint}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nDihentikan oleh pengguna.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
