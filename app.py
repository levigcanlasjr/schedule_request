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
from datetime import date, timedelta

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text

# ---------- Settings ----------
START = date(2026, 10, 24)
END = date(2026, 11, 3)
DAYS = [START + timedelta(days=i) for i in range((END - START).days + 1)]

AM = "5:00 AM - 2:00 PM"
PM = "2:00 PM - 11:00 PM"
OFF = "OFF"
SHIFTS = [AM, PM, OFF]

ROLES = ["Employee", "Approver", "Admin"]
GROUPS = ["HGT", "HPT", "HID"]

st.set_page_config(page_title="Schedule Change Requests", layout="wide")


# ---------- Database ----------
@st.cache_resource
def engine():
    url = os.environ.get("DATABASE_URL", "sqlite:///shift_requests.db")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
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
    try:  # adds the group column to a database made by the first version
        run("ALTER TABLE users ADD COLUMN team TEXT")
    except Exception:
        pass
    if not rows("SELECT username FROM users LIMIT 1"):
        run(
            "INSERT INTO users (username, name, password_hash, role) VALUES (:u, :n, :p, 'Admin')",
            u=os.environ.get("ADMIN_USERNAME", "admin"),
            n="Admin",
            p=hash_password(os.environ.get("ADMIN_PASSWORD", "change-me")),
        )
    return True


# ---------- Shared pieces ----------
def day_label(iso):
    return date.fromisoformat(iso).strftime("%a, %b %d")


def summary_table(team=None):
    """Counts Employees only. Admins and Approvers are left out."""
    who = "u.role = 'Employee'" + (" AND u.team = :t" if team else "")
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
        "Counts employees only. Change requests counts pending requests only. "
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
                }
                st.rerun()
            st.error("Wrong username or password.")


def my_schedule_page(user):
    st.header("My Schedule")
    u = user["username"]
    sched = {r["day"]: r["shift"] for r in rows("SELECT day, shift FROM schedule WHERE username = :u", u=u)}
    reqs = {r["day"]: r for r in rows("SELECT * FROM requests WHERE username = :u", u=u)}

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
            c[3].write(f'{r["status"]}: {r["requested_shift"]}' if r else "")
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
                    "requested_shift = :s, status = 'Pending', decided_by = NULL",
                    u=u, d=k, s=choice,
                )
        st.rerun()

    st.subheader("Team totals")
    show_summary()


def approvals_page(user):
    st.header("Approvals")
    show_summary()

    st.subheader("Pending requests")
    pending = rows(
        "SELECT r.username, u.name, u.team, r.day, r.requested_shift, s.shift AS actual "
        "FROM requests r "
        "JOIN users u ON u.username = r.username "
        "LEFT JOIN schedule s ON s.username = r.username AND s.day = r.day "
        "WHERE r.status = 'Pending' AND u.role = 'Employee' ORDER BY r.day, u.team, u.name"
    )
    if not pending:
        st.write("No pending requests.")
        return

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
                        "UPDATE requests SET status = 'Approved', decided_by = :by "
                        "WHERE username = :u AND day = :d"
                    ),
                    {"by": user["username"], "u": p["username"], "d": p["day"]},
                )
            st.rerun()
        if c[6].button("Reject", key=f"no_{key}"):
            run(
                "UPDATE requests SET status = 'Rejected', decided_by = :by "
                "WHERE username = :u AND day = :d",
                by=user["username"], u=p["username"], d=p["day"],
            )
            st.rerun()


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
        password = st.text_input("Password", type="password")
        role = st.selectbox("Role", ROLES)
        team = st.selectbox("Group", GROUPS)
        if st.form_submit_button("Add"):
            if not (name and username and password):
                st.error("Fill in all fields.")
            elif rows("SELECT username FROM users WHERE username = :u", u=username):
                st.error("That username is already taken.")
            else:
                run(
                    "INSERT INTO users (username, name, password_hash, role, team) "
                    "VALUES (:u, :n, :p, :r, :t)",
                    u=username, n=name, p=hash_password(password), r=role, t=team,
                )
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
            "Group", GROUPS, index=GROUPS.index(current_team) if current_team in GROUPS else None
        )
        new_password = st.text_input("New password (leave blank to keep)", type="password")
        clear = st.checkbox("Clear this user's schedule and requests")
        remove = st.checkbox("Delete this user")
        if st.form_submit_button("Apply"):
            is_self = target == user["username"]
            if is_self and (remove or new_role != "Admin"):
                st.error("You cannot delete yourself or remove your own Admin role.")
            else:
                if clear or remove:
                    run("DELETE FROM schedule WHERE username = :u", u=target)
                    run("DELETE FROM requests WHERE username = :u", u=target)
                if remove:
                    run("DELETE FROM users WHERE username = :u", u=target)
                else:
                    run(
                        "UPDATE users SET role = :r, team = :t WHERE username = :u",
                        r=new_role, t=new_team, u=target,
                    )
                    if new_password:
                        run(
                            "UPDATE users SET password_hash = :p WHERE username = :u",
                            p=hash_password(new_password), u=target,
                        )
                st.rerun()


# ---------- Main ----------
def main():
    init_db()
    user = st.session_state.get("user")
    if not user:
        login_page()
        return

    if user["role"] in ("Approver", "Admin"):
        pages = ["Approvals", "Users"]
    else:
        pages = ["My Schedule"]

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
    else:
        users_page(user)


main()
