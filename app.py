from flask import Flask, render_template, request
from playwright.sync_api import sync_playwright
import re

app = Flask(__name__)


def parse_num(raw: str) -> int:
    """
    Convert '803', '2.3K', '1.1M' → int.
    """
    raw = raw.strip().replace(",", "").lower()
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*([km])?$", raw)
    if not m:
        digits = re.sub(r"[^0-9]", "", raw)
        return int(digits) if digits else 0

    num = float(m.group(1))
    suffix = m.group(2)

    if suffix == "k":
        num *= 1_000
    elif suffix == "m":
        num *= 1_000_000

    return int(num)


def extract_from_dom_payload(payload: dict) -> dict:
    """
    Turn JS-evaluated payload into likes / comments / shares numbers.
    """
    likes = comments = shares = 0

    # ---- Total reactions from "All reactions:" ----
    candidates = payload.get("total_reactions_candidates") or []
    primary_text = payload.get("total_reactions_text") or ""
    if primary_text:
        candidates.append(primary_text)

    total_reacts_val = 0
    for c in candidates:
        v = parse_num(c)
        if 0 < v < 1_000_000 and v > total_reacts_val:
            total_reacts_val = v

    # ---- Per-reaction breakdown (Like: 375, Love: 361, Care: 43, etc.) ----
    reaction_labels = payload.get("reaction_labels") or []
    sum_reacts = 0
    for label in reaction_labels:
        # e.g. "Like: 375 people"
        m = re.search(r":\s*([0-9.,KkMm]+)\s+people", label, re.IGNORECASE)
        if not m:
            continue
        sum_reacts += parse_num(m.group(1))

    # Prefer the explicit "All reactions" value; fallback to summed reactions
    if total_reacts_val > 0:
        likes = total_reacts_val
    else:
        likes = sum_reacts

    full_text = payload.get("full_text") or ""

    # ---- Comments ----
    comment_texts = payload.get("comment_texts") or []
    if comment_texts:
        comments = max(parse_num(t) for t in comment_texts)
    else:
        vals = [
            parse_num(m.group(1))
            for m in re.finditer(r"([0-9][0-9.,KkMm]*)\s+comments?", full_text, re.IGNORECASE)
        ]
        if vals:
            comments = max(vals)

    # ---- Shares ----
    share_texts = payload.get("share_texts") or []
    if share_texts:
        shares = max(parse_num(t) for t in share_texts)
    else:
        vals = [
            parse_num(m.group(1))
            for m in re.finditer(r"([0-9][0-9.,KkMm]*)\s+shares?", full_text, re.IGNORECASE)
        ]
        if vals:
            shares = max(vals)

    total = likes + comments + shares

    return {
        "likes": likes,
        "comments": comments,
        "shares": shares,
        "total": total,
    }


def scrape_post(url: str) -> dict:
    """
    Use Playwright to open the Facebook URL and pull a compact DOM payload
    from JavaScript, then convert it into engagement numbers.
    """
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0 Safari/537.36"
                ),
                locale="en-US",
            )
            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=45000)
            page.wait_for_timeout(3000)

            payload = page.evaluate(
                """
                () => {
                    const out = {
                        reaction_labels: [],
                        comment_texts: [],
                        share_texts: [],
                        full_text: '',
                        total_reactions_text: '',
                        total_reactions_candidates: []
                    };

                    // ----- 1) Individual reaction labels (Like: 375 people, etc.) -----
                    const reactionWords = ['Like','Love','Care','Haha','Wow','Sad','Angry'];
                    const nodes = Array.from(
                        document.querySelectorAll('[aria-label*=" people"]')
                    );
                    for (const node of nodes) {
                        const label = node.getAttribute('aria-label') || '';
                        const m = label.match(/^([A-Za-z]+):\\s*([0-9.,]+)\\s*people/i);
                        if (!m) continue;
                        if (!reactionWords.includes(m[1])) continue;
                        out.reaction_labels.push(label);
                    }

                    // ----- 2) All "All reactions:" blocks -----
                    const allDivs = Array.from(document.querySelectorAll('div'));
                    for (const div of allDivs) {
                        const txt = (div.textContent || '').trim();
                        if (txt === 'All reactions:') {
                            const parent = div.parentElement;
                            if (!parent) continue;
                            const spans = parent.querySelectorAll('span.x135b78x');
                            for (const s of spans) {
                                const numTxt = (s.textContent || '').trim();
                                if (numTxt) {
                                    out.total_reactions_candidates.push(numTxt);
                                }
                            }
                        }
                    }
                    // keep the last seen candidate as "primary" (not critical, but useful)
                    if (out.total_reactions_candidates.length > 0) {
                        out.total_reactions_text =
                            out.total_reactions_candidates[out.total_reactions_candidates.length - 1];
                    }

                    // ----- 3) Comment & share numbers via sprite icons -----
                    const icons = Array.from(
                        document.querySelectorAll('i[data-visualcompletion="css-img"]')
                    );

                    for (const icon of icons) {
                        const style = icon.getAttribute('style') || '';
                        let kind = null;

                        // From your snippet:
                        // background-position: 0px -1037px  => comments
                        // background-position: 0px -1054px  => shares
                        if (style.includes('-1037px')) {
                            kind = 'comment';
                        } else if (style.includes('-1054px')) {
                            kind = 'share';
                        }

                        if (!kind) continue;

                        const iconWrapper = icon.closest('div');
                        if (!iconWrapper) continue;
                        const group = iconWrapper.parentElement;
                        if (!group) continue;

                        const span = group.querySelector('span');
                        if (!span) continue;

                        const raw = (span.textContent || '').trim();
                        if (!raw) continue;

                        if (kind === 'comment') {
                            out.comment_texts.push(raw);
                        } else if (kind === 'share') {
                            out.share_texts.push(raw);
                        }
                    }

                    // ----- 4) Full body text for regex fallbacks -----
                    out.full_text = document.body.innerText || '';

                    return out;
                }
                """
            )

            browser.close()

    except Exception as e:
        return {
            "likes": 0,
            "comments": 0,
            "shares": 0,
            "total": 0,
            "status": f"Browser error: {e}",
        }

    data = extract_from_dom_payload(payload)

    if data["total"] == 0:
        data["status"] = "Could not detect engagement"
    else:
        data["status"] = "OK"

    return data


@app.route("/", methods=["GET", "POST"])
def index():
    results = []
    grand_total = 0

    if request.method == "POST":
        raw_urls = request.form.get("urls", "")
        urls = [u.strip() for u in raw_urls.splitlines() if u.strip()]

        for url in urls:
            engagement = scrape_post(url)
            results.append(
                {
                    "url": url,
                    "likes": engagement["likes"],
                    "comments": engagement["comments"],
                    "shares": engagement["shares"],
                    "total": engagement["total"],
                    "status": engagement["status"],
                }
            )
            grand_total += engagement["total"]

    return render_template("index.html", results=results, grand_total=grand_total)


if __name__ == "__main__":
    app.run(debug=True)
