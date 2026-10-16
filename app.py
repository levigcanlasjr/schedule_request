"""Schedule Change Request app.

Run locally:  streamlit run app.py
Database:     set DATABASE_URL to your Neon connection string.
              Without it, the app uses a local SQLite file for testing.
First login:  set ADMIN_USERNAME and ADMIN_PASSWORD before the first run.
              Defaults are admin / change-me.
"""
import hashlib
import hmac
import os
import secrets
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text

# ---------- Settings ----------
START = date(2026, 10, 24)
END = date(2026, 11, 6)
# End of the first schedule period. Anyone complete up to this date still counts as Submitted.
FIRST_END = date(2026, 11, 3)
DAYS = [START + timedelta(days=i) for i in range((END - START).days + 1)]

AM = "5:00 AM - 2:00 PM"
PM = "2:00 PM - 11:00 PM"
OFF = "OFF"
SHIFTS = [AM, PM, OFF]

ROLES = ["Employee", "Group Leader", "Approver", "Admin"]
# Roles that submit schedules and are counted in totals
COUNTED = "('Employee', 'Group Leader')"
GROUPS = ["HGT", "HPT", "HID"]  # groups that hold employees and group leaders
ALL_GROUPS = GROUPS + ["HiEx"]  # HiEx is for approvers and admin
LOCAL_TZ = timezone(timedelta(hours=8))

st.set_page_config(page_title="Schedule Change Requests", layout="wide")


# ---------- Database ----------
@st.cache_resource
def engine():
    url = os.environ.get("DATABASE_URL", "sqlite:///shift_requests.db")
    if url.startswith("postgres"):  # accept any Postgres URL form and use the psycopg 3 driver
        url = "postgresql+psycopg://" + url.split("://", 1)[1]
    return create_engine(url, pool_pre_ping=True)


def run(sql, **params):
    with engine().begin() as conn:
        conn.execute(text(sql), params)


def rows(sql, **params):
    with engine().connect() as conn:
        return [dict(r._mapping) for r in conn.execute(text(sql), params)]


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return f"{salt}${digest.hex()}"


def check_password(password, stored):
    salt = stored.split("$", 1)[0]
    return hmac.compare_digest(hash_password(password, salt), stored)


@st.cache_resource
def init_db():
    run(
        """CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL)"""
    )
    run(
        """CREATE TABLE IF NOT EXISTS schedule (
            username TEXT NOT NULL,
            day TEXT NOT NULL,
            shift TEXT NOT NULL,
            PRIMARY KEY (username, day))"""
    )
    run(
        """CREATE TABLE IF NOT EXISTS requests (
            username TEXT NOT NULL,
            day TEXT NOT NULL,
            requested_shift TEXT NOT NULL,
            status TEXT NOT NULL,
            decided_by TEXT,
            PRIMARY KEY (username, day))"""
    )
    run(
        """CREATE TABLE IF NOT EXISTS attendance (
            username TEXT NOT NULL,
            day TEXT NOT NULL,
            status TEXT NOT NULL,
            marked_by TEXT,
            PRIMARY KEY (username, day))"""
    )
    try:  # adds the time a request was approved or rejected
        run("ALTER TABLE requests ADD COLUMN decided_at TEXT")
    except Exception:
        pass
    try:  # adds the group column to a database made by the first version
        run("ALTER TABLE users ADD COLUMN team TEXT")
    except Exception:
        pass
    try:  # adds the forced password change flag; existing non-admin users get flagged
        run("ALTER TABLE users ADD COLUMN must_change INTEGER DEFAULT 1")
        run("UPDATE users SET must_change = 0 WHERE role = 'Admin'")
    except Exception:
        pass
    if not rows("SELECT username FROM users LIMIT 1"):
        run(
            "INSERT INTO users (username, name, password_hash, role, must_change) "
            "VALUES (:u, :n, :p, 'Admin', 0)",
            u=os.environ.get("ADMIN_USERNAME", "admin"),
            n="Admin",
            p=hash_password(os.environ.get("ADMIN_PASSWORD", "change-me")),
        )
    return True


# ---------- Shared pieces ----------
def now_local():
    return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")


def decision_note(status, by_name, at):
    """Example: Approved by Maria Cruz, Oct 17 9:30 PM"""
    note = f"{status} by {by_name or 'unknown'}"
    if at:
        try:
            note += ", " + datetime.strptime(at, "%Y-%m-%d %H:%M").strftime("%b %d %I:%M %p").replace(" 0", " ")
        except ValueError:
            pass
    return note


def group_error(role, team):
    if role in ("Employee", "Group Leader") and team not in GROUPS:
        return f"{role} must be in {', '.join(GROUPS)}."
    if team and team not in ALL_GROUPS:
        return f"Group {team} is not valid."
    return None


def day_label(iso):
    return date.fromisoformat(iso).strftime("%a, %b %d")


def summary_table(team=None):
    """Counts Employees only. Admins and Approvers are left out."""
    who = f"u.role IN {COUNTED}" + (" AND u.team = :t" if team else "")
    params = {"t": team} if team else {}
    total = rows(f"SELECT COUNT(*) AS n FROM users u WHERE {who}", **params)[0]["n"]
    sched = rows(
        "SELECT s.day, s.shift, COUNT(*) AS n FROM schedule s "
        f"JOIN users u ON u.username = s.username WHERE {who} GROUP BY s.day, s.shift",
        **params,
    )
    reqs = rows(
        "SELECT r.day, r.requested_shift, COUNT(*) AS n FROM requests r "
        f"JOIN users u ON u.username = r.username WHERE {who} AND r.status = 'Pending' "
        "GROUP BY r.day, r.requested_shift",
        **params,
    )
    s = {(r["day"], r["shift"]): r["n"] for r in sched}
    q = {(r["day"], r["requested_shift"]): r["n"] for r in reqs}
    data = []
    for d in DAYS:
        k = d.isoformat()
        counts = [s.get((k, sh), 0) for sh in SHIFTS]
        asked = [q.get((k, sh), 0) for sh in SHIFTS]
        data.append(
            {
                "Date": day_label(k),
                AM: counts[0],
                PM: counts[1],
                OFF: counts[2],
                "Not set": total - sum(counts),
                "Change requests": sum(asked),
                "Asking for 5 AM": asked[0],
                "Asking for 2 PM": asked[1],
                "Asking for OFF": asked[2],
            }
        )
    return pd.DataFrame(data)


def show_summary():
    tabs = st.tabs(["All groups"] + GROUPS)
    for tab, team in zip(tabs, [None] + GROUPS):
        with tab:
            st.dataframe(summary_table(team), hide_index=True)
    st.caption(
        "Counts employees and group leaders only. Change requests counts pending requests only. "
        "An employee with no group shows under All groups only."
    )


# ---------- Pages ----------
def login_page():
    st.title("Schedule Change Requests")
    with st.form("login"):
        username = st.text_input("Username").strip().lower()
        password = st.text_input("Password", type="password")
        if st.form_submit_button("Log in"):
            found = rows("SELECT * FROM users WHERE lower(username) = :u", u=username)
            if found and check_password(password, found[0]["password_hash"]):
                st.session_state.user = {
                    "username": found[0]["username"],
                    "name": found[0]["name"],
                    "role": found[0]["role"],
                    "team": found[0]["team"],
                    "must_change": bool(found[0]["must_change"]),
                }
                st.rerun()
            st.error("Wrong username or password.")


def change_password_page(user, forced=False):
    st.header("Change Password")
    if forced:
        st.info("You are using a temporary password. Set your own password to continue.")
    with st.form("change_password", clear_on_submit=True):
        current = st.text_input("Current password", type="password")
        new = st.text_input("New password", type="password")
        again = st.text_input("Confirm new password", type="password")
        if st.form_submit_button("Change password"):
            found = rows("SELECT password_hash FROM users WHERE username = :u", u=user["username"])
            if not found or not check_password(current, found[0]["password_hash"]):
                st.error("Current password is wrong.")
            elif len(new) < 8:
                st.error("New password must be at least 8 characters.")
            elif new != again:
                st.error("The new passwords do not match.")
            elif new == current:
                st.error("New password must be different from the current one.")
            else:
                run(
                    "UPDATE users SET password_hash = :p, must_change = 0 WHERE username = :u",
                    p=hash_password(new), u=user["username"],
                )
                st.session_state.user["must_change"] = False
                st.session_state.password_changed = True
                st.rerun()
    if st.session_state.pop("password_changed", False):
        st.success("Password changed.")


def my_schedule_page(user):
    st.header("My Schedule")
    u = user["username"]
    sched = {r["day"]: r["shift"] for r in rows("SELECT day, shift FROM schedule WHERE username = :u", u=u)}
    reqs = {
        r["day"]: r
        for r in rows(
            "SELECT r.*, d.name AS decided_name FROM requests r "
            "LEFT JOIN users d ON d.username = r.decided_by WHERE r.username = :u",
            u=u,
        )
    }

    st.caption(
        "Set your actual schedule first. Once saved, it is locked. "
        "To change it, pick a shift under Request and wait for approval."
    )

    new_actual, new_request, pending_now = {}, {}, {}
    with st.form("mine"):
        h = st.columns([2, 3, 3, 3])
        h[0].markdown("**Date**")
        h[1].markdown("**Actual schedule**")
        h[2].markdown("**Request**")
        h[3].markdown("**Request status**")
        for d in DAYS:
            k = d.isoformat()
            c = st.columns([2, 3, 3, 3])
            c[0].write(day_label(k))
            actual = sched.get(k)
            if actual:
                c[1].write(actual)
            else:
                new_actual[k] = c[1].selectbox(
                    "Actual", ["Not set"] + SHIFTS, key=f"a_{k}", label_visibility="collapsed"
                )
            r = reqs.get(k)
            pending = r["requested_shift"] if r and r["status"] == "Pending" else None
            if actual:
                opts = ["No request"] + [s for s in SHIFTS if s != actual]
                new_request[k] = c[2].selectbox(
                    "Request",
                    opts,
                    index=opts.index(pending) if pending in opts else 0,
                    key=f"r_{k}_{actual}",
                    label_visibility="collapsed",
                )
                pending_now[k] = pending
            else:
                c[2].caption("Set actual schedule first")
            if r and r["status"] in ("Approved", "Rejected"):
                c[3].write(f'{r["requested_shift"]}: {decision_note(r["status"], r["decided_name"], r["decided_at"])}')
            elif r:
                c[3].write(f'{r["status"]}: {r["requested_shift"]}')
        saved = st.form_submit_button("Save")

    if saved:
        for k, shift in new_actual.items():
            if shift != "Not set":
                run(
                    "INSERT INTO schedule (username, day, shift) VALUES (:u, :d, :s) "
                    "ON CONFLICT (username, day) DO NOTHING",
                    u=u, d=k, s=shift,
                )
        for k, choice in new_request.items():
            if choice == "No request":
                if pending_now[k]:
                    run(
                        "DELETE FROM requests WHERE username = :u AND day = :d AND status = 'Pending'",
                        u=u, d=k,
                    )
            elif choice != pending_now[k]:
                run(
                    "INSERT INTO requests (username, day, requested_shift, status, decided_by) "
                    "VALUES (:u, :d, :s, 'Pending', NULL) "
                    "ON CONFLICT (username, day) DO UPDATE SET "
                    "requested_shift = :s, status = 'Pending', decided_by = NULL, decided_at = NULL",
                    u=u, d=k, s=choice,
                )
        st.rerun()

    st.subheader("Team totals")
    show_summary()


def can_decide(user, request_owner, owner_team):
    if user["role"] in ("Approver", "Admin"):
        return True
    if user["role"] == "Group Leader":
        return owner_team == user["team"] and request_owner != user["username"]
    return False


def approvals_page(user):
    st.header("Approvals")
    leader = user["role"] == "Group Leader"
    if leader:
        st.caption(f'Showing your group only: {user["team"] or "no group set"}')
        st.dataframe(summary_table(user["team"]), hide_index=True)
    else:
        show_summary()

    params = {}
    team_filter = ""
    if leader:
        team_filter = " AND u.team = :t"
        params["t"] = user["team"]

    st.subheader("Pending requests")
    pending = rows(
        "SELECT r.username, u.name, u.team, r.day, r.requested_shift, s.shift AS actual "
        "FROM requests r "
        "JOIN users u ON u.username = r.username "
        "LEFT JOIN schedule s ON s.username = r.username AND s.day = r.day "
        f"WHERE r.status = 'Pending' AND u.role IN {COUNTED}{team_filter} ORDER BY r.day, u.team, u.name",
        **params,
    )
    if not pending:
        st.write("No pending requests.")
    else:
        if leader:
            st.caption("Your own requests go to an approver.")
        widths = [3, 1, 2, 3, 3, 1, 1]
        h = st.columns(widths)
        for col, label in zip(h, ["Name", "Group", "Date", "Actual", "Requested", "", ""]):
            col.markdown(f"**{label}**")
        for p in pending:
            key = f'{p["username"]}_{p["day"]}'
            c = st.columns(widths)
            c[0].write(p["name"])
            c[1].write(p["team"] or "")
            c[2].write(day_label(p["day"]))
            c[3].write(p["actual"] or "Not set")
            c[4].write(p["requested_shift"])
            if not can_decide(user, p["username"], p["team"]):
                c[5].caption("Needs an approver")
                continue
            if c[5].button("Approve", key=f"ok_{key}"):
                with engine().begin() as conn:
                    conn.execute(
                        text(
                            "INSERT INTO schedule (username, day, shift) VALUES (:u, :d, :s) "
                            "ON CONFLICT (username, day) DO UPDATE SET shift = :s"
                        ),
                        {"u": p["username"], "d": p["day"], "s": p["requested_shift"]},
                    )
                    conn.execute(
                        text(
                            "UPDATE requests SET status = 'Approved', decided_by = :by, decided_at = :at "
                            "WHERE username = :u AND day = :d"
                        ),
                        {"by": user["username"], "at": now_local(), "u": p["username"], "d": p["day"]},
                    )
                st.rerun()
            if c[6].button("Reject", key=f"no_{key}"):
                run(
                    "UPDATE requests SET status = 'Rejected', decided_by = :by, decided_at = :at "
                    "WHERE username = :u AND day = :d",
                    by=user["username"], at=now_local(), u=p["username"], d=p["day"],
                )
                st.rerun()

    st.subheader("Recent decisions")
    decided = rows(
        "SELECT u.name, u.team, r.day, r.requested_shift, r.status, r.decided_at, d.name AS decided_name "
        "FROM requests r JOIN users u ON u.username = r.username "
        "LEFT JOIN users d ON d.username = r.decided_by "
        f"WHERE r.status IN ('Approved', 'Rejected'){team_filter} "
        "ORDER BY r.decided_at DESC LIMIT 100",
        **params,
    )
    if not decided:
        st.write("No decisions yet.")
    else:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Name": d["name"],
                        "Group": d["team"] or "",
                        "Date": day_label(d["day"]),
                        "Requested": d["requested_shift"],
                        "Decision": decision_note(d["status"], d["decided_name"], d["decided_at"]),
                    }
                    for d in decided
                ]
            ),
            hide_index=True,
        )


SHORT = {AM: "5AM-2PM", PM: "2PM-11PM", OFF: "OFF"}


def all_schedules_page(user):
    st.header("All Schedules")
    leader = user["role"] == "Group Leader"
    params = {}
    where = f"u.role IN {COUNTED}"
    if leader:
        where += " AND u.team = :t"
        params["t"] = user["team"]
    people = rows(f"SELECT u.username, u.name, u.team FROM users u WHERE {where} ORDER BY u.team, u.name", **params)
    sched = {
        (r["username"], r["day"]): r["shift"]
        for r in rows(f"SELECT s.username, s.day, s.shift FROM schedule s JOIN users u ON u.username = s.username WHERE {where}", **params)
    }
    pend = {
        (r["username"], r["day"]): r["requested_shift"]
        for r in rows(
            "SELECT r.username, r.day, r.requested_shift FROM requests r JOIN users u ON u.username = r.username "
            f"WHERE {where} AND r.status = 'Pending'",
            **params,
        )
    }

    approved = {
        (r["username"], r["day"]): r["decided_name"]
        for r in rows(
            "SELECT r.username, r.day, d.name AS decided_name FROM requests r "
            "JOIN users u ON u.username = r.username LEFT JOIN users d ON d.username = r.decided_by "
            f"WHERE {where} AND r.status = 'Approved'",
            **params,
        )
    }
    data = []
    for p in people:
        line = {"Name": p["name"], "Group": p["team"] or ""}
        for d in DAYS:
            k = d.isoformat()
            cell = SHORT.get(sched.get((p["username"], k)), "")
            if (p["username"], k) in pend:
                cell = f'{cell} (req: {SHORT[pend[(p["username"], k)]]})'.strip()
            elif (p["username"], k) in approved:
                cell = f'{cell} (approved by {approved[(p["username"], k)] or "unknown"})'.strip()
            line[d.strftime("%a %b %d")] = cell
        data.append(line)
    df = pd.DataFrame(data, columns=["Name", "Group"] + [d.strftime("%a %b %d") for d in DAYS])

    if leader:
        st.caption(f'Showing your group only: {user["team"] or "no group set"}')
    else:
        team = st.selectbox("Group", ["All groups"] + GROUPS, key="all_sched_group")
        if team != "All groups":
            df = df[df["Group"] == team]
    st.caption("A blank cell means the day is not set. (req: ...) shows a pending change request. "
        "(approved by ...) shows who approved the change for that day.")
    st.dataframe(df, hide_index=True)
    st.download_button(
        "Download this table (CSV)", df.to_csv(index=False), file_name="all_schedules.csv", mime="text/csv"
    )

    if user["role"] != "Admin" or not people:
        return

    st.subheader("Edit an employee's schedule")
    st.caption(
        "Changes to the actual schedule save right away and skip approval. "
        "Pending requests stay open for an approver. You can also add or remove a request for this person."
    )
    names = {p["username"]: p for p in people}
    target = st.selectbox(
        "Employee",
        list(names),
        format_func=lambda x: f'{names[x]["name"]} ({names[x]["team"] or "no group"})',
        key="edit_sched_target",
    )
    opts_actual = ["Not set"] + SHIFTS
    opts_req = ["No request"] + SHIFTS
    new_actual, new_req = {}, {}
    with st.form(f"admin_sched_{target}"):
        h = st.columns([2, 3, 3])
        h[0].markdown("**Date**")
        h[1].markdown("**Actual schedule**")
        h[2].markdown("**Pending request**")
        for d in DAYS:
            k = d.isoformat()
            c = st.columns([2, 3, 3])
            c[0].write(day_label(k))
            cur = sched.get((target, k))
            new_actual[k] = c[1].selectbox(
                "Actual", opts_actual, index=opts_actual.index(cur) if cur in opts_actual else 0,
                key=f"aa_{target}_{k}", label_visibility="collapsed",
            )
            curr = pend.get((target, k))
            new_req[k] = c[2].selectbox(
                "Request", opts_req, index=opts_req.index(curr) if curr in opts_req else 0,
                key=f"ar_{target}_{k}", label_visibility="collapsed",
            )
        saved = st.form_submit_button("Save changes")

    if saved:
        changed = 0
        for k in new_actual:
            cur, new = sched.get((target, k)), new_actual[k]
            if new == "Not set" and cur:
                run("DELETE FROM schedule WHERE username = :u AND day = :d", u=target, d=k)
                changed += 1
            elif new != "Not set" and new != cur:
                run(
                    "INSERT INTO schedule (username, day, shift) VALUES (:u, :d, :s) "
                    "ON CONFLICT (username, day) DO UPDATE SET shift = :s",
                    u=target, d=k, s=new,
                )
                changed += 1
            curr, newr = pend.get((target, k)), new_req[k]
            if newr == curr or (newr == "No request" and not curr):
                continue
            if newr == "No request":
                run(
                    "DELETE FROM requests WHERE username = :u AND day = :d AND status = 'Pending'",
                    u=target, d=k,
                )
            else:
                run(
                    "INSERT INTO requests (username, day, requested_shift, status, decided_by) "
                    "VALUES (:u, :d, :s, 'Pending', NULL) "
                    "ON CONFLICT (username, day) DO UPDATE SET "
                    "requested_shift = :s, status = 'Pending', decided_by = NULL, decided_at = NULL",
                    u=target, d=k, s=newr,
                )
            changed += 1
        st.session_state.admin_sched_msg = f"Saved {changed} change(s) for {names[target]['name']}."
        st.rerun()
    if "admin_sched_msg" in st.session_state:
        st.success(st.session_state.pop("admin_sched_msg"))


ATTENDANCE = ["Not marked", "Present", "Late", "Absent", "On leave"]


def by_day_page(user):
    st.header("By Day")
    leader = user["role"] == "Group Leader"
    params = {}
    where = f"u.role IN {COUNTED}"
    if leader:
        where += " AND u.team = :t"
        params["t"] = user["team"]
    people = rows(f"SELECT u.username, u.name, u.team FROM users u WHERE {where} ORDER BY u.name", **params)
    sched = {
        (r["username"], r["day"]): r["shift"]
        for r in rows(f"SELECT s.username, s.day, s.shift FROM schedule s JOIN users u ON u.username = s.username WHERE {where}", **params)
    }
    pend = {
        (r["username"], r["day"]): r["requested_shift"]
        for r in rows(
            "SELECT r.username, r.day, r.requested_shift FROM requests r JOIN users u ON u.username = r.username "
            f"WHERE {where} AND r.status = 'Pending'",
            **params,
        )
    }
    att = {
        (r["username"], r["day"]): r["status"]
        for r in rows(f"SELECT a.username, a.day, a.status FROM attendance a JOIN users u ON u.username = a.username WHERE {where}", **params)
    }

    groups = [user["team"]] if leader else GROUPS + (["No group"] if any(not p["team"] for p in people) else [])
    def members(g):
        return [p for p in people if (p["team"] or "No group") == g]

    if leader:
        st.caption(f'Showing your group only: {user["team"] or "no group set"}')

    # ---------- One day ----------
    today = date.today()
    default = DAYS.index(today) if today in DAYS else 0
    day = st.selectbox("Date", DAYS, index=default, format_func=lambda d: d.strftime("%a, %b %d"))
    k = day.isoformat()
    export = []
    for g in groups:
        team_people = members(g)
        st.subheader(f"{g} ({len(team_people)})")
        by_shift = {sh: [] for sh in SHIFTS + ["Not set"]}
        for p in team_people:
            by_shift[sched.get((p["username"], k), "Not set")].append(p)
        cols = st.columns(4)
        for col, sh in zip(cols, SHIFTS + ["Not set"]):
            col.metric(sh, len(by_shift[sh]))

        with st.form(f"att_{g}_{k}"):
            picks = {}
            for sh in [AM, PM]:
                on_shift = by_shift[sh]
                marked = [att.get((p["username"], k), "Not marked") for p in on_shift]
                tally = ", ".join(f"{m} {marked.count(m)}" for m in ATTENDANCE[1:] if marked.count(m))
                st.markdown(f"**{sh}** ({len(on_shift)} scheduled{'; ' + tally if tally else ''})")
                if not on_shift:
                    st.caption("No one scheduled.")
                for p in on_shift:
                    label = p["name"]
                    if (p["username"], k) in pend:
                        label += f' (req: {SHORT[pend[(p["username"], k)]]})'
                    c = st.columns([3, 2])
                    c[0].write(label)
                    cur = att.get((p["username"], k), "Not marked")
                    picks[p["username"]] = c[1].selectbox(
                        "Attendance", ATTENDANCE, index=ATTENDANCE.index(cur),
                        key=f"att_{p['username']}_{k}", label_visibility="collapsed",
                    )
            for sh in [OFF, "Not set"]:
                names = []
                for p in by_shift[sh]:
                    label = p["name"]
                    if (p["username"], k) in pend:
                        label += f' (req: {SHORT[pend[(p["username"], k)]]})'
                    names.append(label)
                st.markdown(f"**{sh}** ({len(names)}): " + (", ".join(names) if names else "none"))
            save = st.form_submit_button(f"Save attendance for {g}")
        if save:
            for u, new in picks.items():
                cur = att.get((u, k))
                if new == "Not marked" and cur:
                    run("DELETE FROM attendance WHERE username = :u AND day = :d", u=u, d=k)
                elif new != "Not marked" and new != cur:
                    run(
                        "INSERT INTO attendance (username, day, status, marked_by) VALUES (:u, :d, :s, :by) "
                        "ON CONFLICT (username, day) DO UPDATE SET status = :s, marked_by = :by",
                        u=u, d=k, s=new, by=user["username"],
                    )
            st.session_state.att_msg = f"Attendance saved for {g}."
            st.rerun()

        for sh in SHIFTS + ["Not set"]:
            for p in by_shift[sh]:
                export.append(
                    {
                        "Date": day_label(k),
                        "Group": g,
                        "Name": p["name"],
                        "Shift": sh,
                        "Pending request": pend.get((p["username"], k), ""),
                        "Attendance": att.get((p["username"], k), "Not marked") if sh in (AM, PM) else "",
                    }
                )
    if "att_msg" in st.session_state:
        st.success(st.session_state.pop("att_msg"))
    st.download_button(
        "Download this day (CSV)",
        pd.DataFrame(export).to_csv(index=False),
        file_name=f"schedule_{k}.csv",
        mime="text/csv",
    )

    # ---------- Full view ----------
    st.subheader("Full view (all days)")
    st.caption("Shift counts per group per day. Attendance counts people on the 5 AM and 2 PM shifts.")
    full = []
    for d in DAYS:
        dk = d.isoformat()
        for g in groups:
            team_people = members(g)
            shifts = [sched.get((p["username"], dk), "Not set") for p in team_people]
            marks = [att.get((p["username"], dk), "Not marked") for p, sh in zip(team_people, shifts) if sh in (AM, PM)]
            line = {"Date": d.strftime("%a %b %d"), "Group": g}
            for sh in SHIFTS + ["Not set"]:
                line[SHORT.get(sh, sh)] = shifts.count(sh)
            for m in ATTENDANCE[1:] + ["Not marked"]:
                line[m] = marks.count(m)
            full.append(line)
    full_df = pd.DataFrame(full)
    if not leader and len(groups) > 1:
        pick = st.selectbox("Group", ["All groups"] + groups, key="full_group")
        if pick != "All groups":
            full_df = full_df[full_df["Group"] == pick]
    st.dataframe(full_df, hide_index=True)
    st.download_button(
        "Download full view (CSV)", full_df.to_csv(index=False), file_name="full_view.csv", mime="text/csv"
    )


def submissions_page(user):
    st.header("Submissions")
    leader = user["role"] == "Group Leader"
    params = {"first": DAYS[0].isoformat(), "last": DAYS[-1].isoformat(), "first_end": FIRST_END.isoformat()}
    where = f"u.role IN {COUNTED}"
    if leader:
        where += " AND u.team = :t"
        params["t"] = user["team"]
    people = rows(
        "SELECT u.name, u.username, u.team, COUNT(s.day) AS days, "
        "COUNT(CASE WHEN s.day <= :first_end THEN 1 END) AS first_days FROM users u "
        "LEFT JOIN schedule s ON s.username = u.username AND s.day BETWEEN :first AND :last "
        f"WHERE {where} GROUP BY u.name, u.username, u.team ORDER BY u.team, u.name",
        **params,
    )
    total_days = len(DAYS)
    first_total = len([d for d in DAYS if d <= FIRST_END])
    first_label = f"Submitted [{FIRST_END.strftime('%b')} {FIRST_END.day}]"

    def status(n, first_n):
        if n >= total_days:
            return "Submitted"
        if first_n >= first_total:
            return first_label
        return "Partially submitted" if n > 0 else "Not submitted"

    df = pd.DataFrame(
        [
            {
                "Name": p["name"],
                "Username": p["username"],
                "Group": p["team"] or "",
                "Days set": f'{p["days"]} of {total_days}',
                "Status": status(p["days"], p["first_days"]),
            }
            for p in people
        ],
        columns=["Name", "Username", "Group", "Days set", "Status"],
    )

    if leader:
        st.caption(f'Showing your group only: {user["team"] or "no group set"}')
    else:
        team = st.selectbox("Group", ["All groups"] + GROUPS)
        if team != "All groups":
            df = df[df["Group"] == team]

    counts = df["Status"].value_counts()
    c = st.columns(3)
    c[0].metric("Submitted", int(counts.get("Submitted", 0)) + int(counts.get(first_label, 0)))
    c[1].metric("Partially submitted", int(counts.get("Partially submitted", 0)))
    c[2].metric("Not submitted", int(counts.get("Not submitted", 0)))

    choices = ["Submitted", first_label, "Partially submitted", "Not submitted"]
    st.caption(f"Submitted includes {first_label}: complete up to {FIRST_END.strftime('%B')} {FIRST_END.day}, but not the later days.")
    picked = st.multiselect("Status", choices, default=choices)
    df = df[df["Status"].isin(picked)]
    st.dataframe(df, hide_index=True)
    st.download_button(
        "Download this list (CSV)",
        df.to_csv(index=False),
        file_name="submission_status.csv",
        mime="text/csv",
    )


def users_page(user):
    st.header("Users")
    people = rows("SELECT username, name, role, team FROM users ORDER BY role, team, name")
    st.dataframe(
        pd.DataFrame(people).rename(
            columns={"username": "Username", "name": "Name", "role": "Role", "team": "Group"}
        ),
        hide_index=True,
    )
    if user["role"] != "Admin":
        st.caption("Only the Admin can add or edit users.")
        return

    st.subheader("Add user")
    with st.form("add_user", clear_on_submit=True):
        name = st.text_input("Full name").strip()
        username = st.text_input("Username").strip().lower()
        password = st.text_input("Temporary password", type="password")
        role = st.selectbox("Role", ROLES)
        team = st.selectbox("Group", ALL_GROUPS)
        if st.form_submit_button("Add"):
            if not (name and username and password):
                st.error("Fill in all fields.")
            elif group_error(role, team):
                st.error(group_error(role, team))
            elif rows("SELECT username FROM users WHERE username = :u", u=username):
                st.error("That username is already taken.")
            else:
                run(
                    "INSERT INTO users (username, name, password_hash, role, team, must_change) "
                    "VALUES (:u, :n, :p, :r, :t, 1)",
                    u=username, n=name, p=hash_password(password), r=role, t=team,
                )
                st.rerun()

    st.subheader("Bulk upload users")
    st.caption(
        "Upload a CSV with the columns Name, Username, Role, Group. "
        f"Role is one of {', '.join(ROLES)} (blank means Employee). "
        f"Group is one of {', '.join(GROUPS)} for employees and group leaders, or HiEx for approvers and admin. "
        "Everyone in the file gets the same temporary password and must change it at first login."
    )
    st.download_button(
        "Download CSV template",
        "Name,Username,Role,Group\nMaria Cruz,maria.cruz,Employee,HGT\n",
        file_name="users_template.csv",
        mime="text/csv",
    )
    for kind, message in st.session_state.pop("bulk_result", []):
        (st.success if kind == "ok" else st.warning)(message)
    with st.form("bulk_upload", clear_on_submit=True):
        upload = st.file_uploader("CSV file", type="csv")
        temp_password = st.text_input("Temporary password for all users in the file", type="password")
        if st.form_submit_button("Upload users"):
            if upload is None or not temp_password:
                st.error("Choose a CSV file and type a temporary password.")
            else:
                try:
                    df = pd.read_csv(upload, dtype=str, encoding="utf-8-sig").fillna("")
                    df.columns = [str(c).strip().lower() for c in df.columns]
                except Exception:
                    df = None
                if df is None or not {"name", "username"} <= set(df.columns):
                    st.error("Could not read the file. It needs at least Name and Username columns.")
                else:
                    taken = {r["username"].lower() for r in rows("SELECT username FROM users")}
                    added, skipped = 0, []
                    with st.spinner("Adding users..."):
                        for i, row in df.iterrows():
                            line = i + 2  # row number as shown in a spreadsheet
                            name = row["name"].strip()
                            username = row["username"].strip().lower()
                            role = row.get("role", "").strip().title() or "Employee"
                            raw_team = row.get("group", "").strip()
                            team = {g.upper(): g for g in ALL_GROUPS}.get(raw_team.upper(), raw_team) or None
                            if not name and not username:
                                continue
                            if not name or not username:
                                skipped.append(f"Row {line}: name or username is blank")
                            elif username in taken:
                                skipped.append(f"Row {line}: username {username} already exists")
                            elif role not in ROLES:
                                skipped.append(f"Row {line}: role {row.get('role', '')} is not valid")
                            elif group_error(role, team):
                                skipped.append(f"Row {line}: {group_error(role, team)}")
                            else:
                                run(
                                    "INSERT INTO users (username, name, password_hash, role, team, must_change) "
                                    "VALUES (:u, :n, :p, :r, :t, 1)",
                                    u=username, n=name, p=hash_password(temp_password), r=role, t=team,
                                )
                                taken.add(username)
                                added += 1
                    result = [("ok", f"Added {added} user(s).")]
                    if skipped:
                        result.append(("skip", "Skipped:  \n" + "  \n".join(skipped)))
                    st.session_state.bulk_result = result
                    st.rerun()

    st.subheader("Edit user")
    by_username = {p["username"]: p for p in people}
    target = st.selectbox(
        "User", list(by_username), format_func=lambda x: f'{by_username[x]["name"]} ({x})'
    )
    current_team = by_username[target]["team"]
    with st.form(f"edit_user_{target}"):
        new_role = st.selectbox("Role", ROLES, index=ROLES.index(by_username[target]["role"]))
        new_team = st.selectbox(
            "Group", ALL_GROUPS, index=ALL_GROUPS.index(current_team) if current_team in ALL_GROUPS else None
        )
        new_password = st.text_input("New temporary password (leave blank to keep)", type="password")
        clear = st.checkbox("Clear this user's schedule and requests")
        remove = st.checkbox("Delete this user")
        if st.form_submit_button("Apply"):
            is_self = target == user["username"]
            if is_self and (remove or new_role != "Admin"):
                st.error("You cannot delete yourself or remove your own Admin role.")
            elif not remove and group_error(new_role, new_team):
                st.error(group_error(new_role, new_team))
            else:
                if clear or remove:
                    run("DELETE FROM schedule WHERE username = :u", u=target)
                    run("DELETE FROM requests WHERE username = :u", u=target)
                    run("DELETE FROM attendance WHERE username = :u", u=target)
                if remove:
                    run("DELETE FROM users WHERE username = :u", u=target)
                else:
                    run(
                        "UPDATE users SET role = :r, team = :t WHERE username = :u",
                        r=new_role, t=new_team, u=target,
                    )
                    if new_password:
                        run(
                            "UPDATE users SET password_hash = :p, must_change = :m WHERE username = :u",
                            p=hash_password(new_password), m=0 if is_self else 1, u=target,
                        )
                st.rerun()


# ---------- Main ----------
def main():
    init_db()
    user = st.session_state.get("user")
    if not user:
        login_page()
        return

    if user.get("must_change"):
        change_password_page(user, forced=True)
        if st.button("Log out"):
            del st.session_state["user"]
            st.rerun()
        return

    if user["role"] in ("Approver", "Admin"):
        pages = ["Approvals", "All Schedules", "By Day", "Submissions", "Users"]
    elif user["role"] == "Group Leader":
        pages = ["My Schedule", "Approvals", "All Schedules", "By Day", "Submissions"]
    else:
        pages = ["My Schedule"]
    pages.append("Change Password")

    with st.sidebar:
        st.write(f'**{user["name"]}**')
        st.caption(user["role"])
        page = st.radio("Menu", pages)
        if st.button("Log out"):
            del st.session_state["user"]
            st.rerun()

    if page == "My Schedule":
        my_schedule_page(user)
    elif page == "Approvals":
        approvals_page(user)
    elif page == "All Schedules":
        all_schedules_page(user)
    elif page == "By Day":
        by_day_page(user)
    elif page == "Submissions":
        submissions_page(user)
    elif page == "Change Password":
        change_password_page(user)
    else:
        users_page(user)


main()
