"""
Kory's Scheduling Rules — default preferences (living document).

These outline Kory's *standard* preferences today. They will change over time.

NOT every key here is machine-enforced. Keys the code actually reads are safe to
edit directly (durations, preferred_times, locations, VENUE_ADDRESSES, HOLD_RULES
day counts). Keys that only feed the LLM prompt text (notes, override_condition,
travel_rule, stack_preference, reservation_note, post_meeting_rule, avoid_days,
hard_end_time, max_per_week inside MEETING_TYPES) shape drafts but do NOT change
deterministic validation — the enforced equivalents live in CAPACITY_LIMITS,
BUFFER_RULES, and app/rules/validators.py. When changing a "hard" rule, verify a
validator reads it before trusting the edit.

Precedence (highest wins):
  1. Kory's calendar (actual busy/free, holds, existing commitments)
  2. What Kory says in Teams or in the email thread (explicit overrides)
  3. These rules (defaults when calendar is open and Kory has not said otherwise)

Soft rules (lunch, weekly caps, etc.) are guidance — Kory can ask for exceptions.
Hard blocks (trainer, Doug, board meetings, etc.) still apply unless Kory explicitly
overrides in chat before you propose or send.
"""

# ─────────────────────────────────────────────────────────────
# DAILY AVAILABILITY WINDOWS
# ─────────────────────────────────────────────────────────────

DAILY_AVAILABILITY = {
    # Monday, Wednesday, Friday — workout days
    "Monday":    {"earliest_virtual_informal": "08:00", "earliest_formal_inperson": "09:30", "latest": "18:00"},
    "Tuesday":   {"earliest_default": "09:00", "earliest_occasional": "07:00", "earliest_eastcoast": "06:00", "latest": "18:00"},
    "Wednesday": {"earliest_virtual_informal": "08:00", "earliest_formal_inperson": "09:30", "latest": "18:00"},
    "Thursday":  {"earliest_default": "09:00", "earliest_occasional": "07:00", "earliest_eastcoast": "06:00", "latest": "18:00"},
    "Friday":    {"earliest_virtual_informal": "08:00", "earliest_formal_inperson": "09:30", "latest": "18:00"},
    "Saturday":  {"available": False, "exception": "Only if Bridget and Maclain are occupied with their own activities. Check family calendar first."},
    "Sunday":    {"available": False, "exception": "Only if Bridget and Maclain are occupied with their own activities. Check family calendar first."},
}

WORKOUT_DAYS = ["Monday", "Wednesday", "Friday"]
EARLY_START_DAYS = ["Tuesday", "Thursday"]  # 6-7 AM allowed occasionally

# ─────────────────────────────────────────────────────────────
# HARD BLOCKS — NEVER SCHEDULE OVER THESE
# ─────────────────────────────────────────────────────────────

HARD_BLOCKS = [
    {
        "name": "Trainer Workout",
        "days": ["Monday", "Wednesday", "Friday"],
        "start": "06:30",
        "end": "08:00",
        "notes": (
            "Non-negotiable. Never override. Scheduling floor is 8:00 — Kory takes 8:00 AM "
            "virtual/informal calls while finishing the workout (in-person pushes to 9:30). "
            "The physical session runs to ~8:30 on the calendar, so an actual "
            "'KM Personal Training' event will conservatively block 8:00–8:30 via overlap when present."
        ),
    },
    {
        "name": "Doug (Executive Coach)",
        "days": ["Monday"],
        "start": "13:15",
        "end": "14:15",
        "frequency": "weekly",
        "notes": "Never override.",
    },
    {
        "name": "Capital Demolition Bi-Weekly",
        "days": ["Thursday"],
        "start": "07:00",
        "end": "08:00",
        "frequency": "bi-weekly",
        "notes": "Currently on calendar at 8 AM but will move to 7 AM. Never override.",
    },
    {
        "name": "YPO Forum",
        "frequency": "monthly, 8x per year (skips summer)",
        "notes": "Hard block. Never override. Confirm exact dates from calendar.",
    },
    {
        "name": "Board Meeting — Canopy Service Partners",
        "frequency": "recurring, involves travel",
        "notes": "Hard block. Never override. Involves travel.",
    },
    {
        "name": "Board Meeting — NextSite",
        "location": "Denver-based",
        "notes": "Hard block. Never override.",
    },
    {
        "name": "HRT Appointment (Dr. Bruice)",
        "frequency": "quarterly",
        "notes": "Very hard to reschedule. Never override.",
    },
    {
        "name": "Kory Drop Off / Pick Up (son)",
        "notes": "Never schedule over. Check calendar for exact times.",
    },
    {
        "name": "Family Google Calendar — 'Do Not Move'",
        "notes": "Anything Bridget has marked 'Do Not Move' is non-negotiable. Always check family calendar.",
    },
]

# ─────────────────────────────────────────────────────────────
# SOFT BLOCKS — PROTECT BUT MOVABLE FOR URGENT REASONS
# ─────────────────────────────────────────────────────────────

SOFT_BLOCKS = [
    {
        "name": "Patrick Weekly Sync",
        "days": ["Friday"],
        "duration_hours": 1,
        "movable": True,
        "override_condition": "Occasionally movable if necessary.",
    },
    {
        "name": "WOB Block (Work On Business / Deep Work)",
        "movable": True,
        "override_condition": "Only override for something urgent like a new client. Do not override routinely.",
        "notes": "Morning hours before noon are Kory's best work time. Protect these.",
    },
    {
        "name": "3 PM Daily Inbox Review",
        "start": "15:00",
        "duration_minutes": 30,
        "movable": True,
        "override_condition": "Keep when feasible. Do not make it a habit to move it.",
    },
]

# ─────────────────────────────────────────────────────────────
# MEETING TYPES — DURATIONS AND RULES
# ─────────────────────────────────────────────────────────────

MEETING_TYPES = {
    "referral_or_intro": {
        "label": "Referral Advocate / General Intro",
        "duration_minutes": 30,
        "format": "virtual_default",
    },
    "new_client": {
        "label": "New Client Meeting",
        "duration_minutes": 60,
        "notes": "These run longer — give them the room. Block the full hour.",
        "format": "virtual_or_inperson",
    },
    "coffee": {
        "label": "Coffee Meeting",
        "duration_minutes": 60,
        "calendar_block_minutes": 90,  # scheduling reserve: 60 min meeting + 30 min post-buffer
        "preferred_times": ["08:30", "09:00"],
        "locations": ["Olive & Finch", "Aviano on St. Paul", "Aviano on Detroit"],
        "location_area": "Cherry Creek only (unless important client requests elsewhere — Kory will advise)",
        "post_meeting_buffer": True,
        "notes": "Never schedule anything immediately after a coffee meeting. They run long.",
        "travel_rule": "If location is far from home, add 30 min for commute each way.",
    },
    "happy_hour": {
        "label": "Happy Hour",
        "duration_minutes": 90,  # 1.5 hours minimum
        "preferred_times": ["15:30", "16:00"],
        "hard_end_time": "18:00",
        "max_per_week": 2,
        "avoid_days": ["Friday"],
        "post_meeting_rule": "Never schedule anything after happy hour — family time.",
        "reservation_required": True,
        "reservation_note": "Request a bar booth.",
        # Clean display names only — these go on the Outlook invite verbatim.
        # Opening-time constraints live in VENUE_ADDRESSES below.
        "locations": [
            "Cherry Creek Grill",
            "Hillstone",
            "Quality Italian",
        ],
    },
    "dinner": {
        "label": "Dinner Meeting",
        "duration_minutes": 90,  # 1.5-2 hours
        "max_per_week": 1,
        "location_preference": "Cherry Creek area",
        "stack_preference": "Strongly prefer to stack on same evening as a happy hour — saves an evening.",
        "notes": "Exception to the 6 PM cutoff rule.",
    },
    "lunch": {
        "label": "Lunch Meeting",
        "duration_minutes": 60,
        "default": "NO — Kory works through lunch by default.",
        "exception": "Only for clients who absolutely cannot meet any other time.",
    },
    "virtual_30": {
        "label": "30-min Virtual/Teams Meeting",
        "duration_minutes": 30,
        "format": "virtual",
        "batching_rule": "Back-to-back is fine. Max 2-hour blocks of back-to-back meetings, then 30-min break.",
    },
    "podcast": {
        "label": "The Turn Podcast Recording",
        "duration_minutes": 30,
        "format": "virtual",
        "scheduling": "Ad hoc — no fixed cadence. Opportunistic booking.",
    },
}

# VENUE ADDRESSES — appended to the Outlook invite Location field so guests
# get a mappable address (matches Heidi's invite convention).
# VERIFY BEFORE RELYING ON THEM: only Aviano on St. Paul is confirmed from a
# real Heidi invite; the rest are believed-correct and need a one-time check.
VENUE_ADDRESSES = {
    "Olive & Finch": "3390 E 1st Ave, Denver, CO 80206",
    "Aviano on St. Paul": "215 Saint Paul St, Denver, CO 80206",
    "Aviano on Detroit": "244 Detroit St, Denver, CO 80206",
    "Cherry Creek Grill": "184 Fillmore St, Denver, CO 80206",
    "Hillstone": "303 Josephine St, Denver, CO 80206",
    "Quality Italian": "241 Columbine St, Denver, CO 80206",
}

# Earliest bookable start per venue (opening time, HH:MM local). Read by
# invite/venue selection so e.g. Quality Italian is never offered for 3:30.
VENUE_OPENS_AT = {
    "Cherry Creek Grill": "15:30",
    "Hillstone": "15:30",
    "Quality Italian": "16:00",
}

# BUFFER AND CAPACITY RULES

BUFFER_RULES = {
    "default_buffer_between_meetings": 0,  # back-to-back is fine
    "max_back_to_back_block_hours": 2,     # then needs a 30-min break
    "break_after_block_minutes": 30,
    "coffee_post_buffer_minutes": 30,      # always 30 min after coffee
    "wob_block_counts_as_break": True,
    "inbox_review_counts_as_break": True,
}

CAPACITY_LIMITS = {
    "happy_hour_per_week": 2,
    "dinner_per_week": 1,
    "lunch": "exception only",
    "travel_weeks": "Only 2-3 critical check-ins when Kory is traveling (typically 1-2 week trips). Keep rest of week clear.",
}

# ─────────────────────────────────────────────────────────────
# TRAVEL TIMES FROM HOME OFFICE (minutes)
# ─────────────────────────────────────────────────────────────

TRAVEL_TIMES = {
    "Cherry Creek":     {"drive_minutes": 15, "notes": "Default location for all in-person Denver meetings."},
    "Downtown Denver":  {"drive_minutes": 20},
    "DTC":              {"drive_minutes": 30},
    "Littleton":        {"drive_minutes": 45},
    "DEN Airport":      {"drive_minutes": 35, "buffer": 45, "notes": "Leave 45 min before to buffer for traffic."},
}

DRIVE_TIME_RULE = (
    "Kory is happy to take calls during drive time. "
    "If driving 30+ minutes, the agent can book a phone call during that drive. "
    "Same for any drive over 15 minutes."
)

# ─────────────────────────────────────────────────────────────
# HOLDS AND PROSPECT MANAGEMENT
# ─────────────────────────────────────────────────────────────

HOLD_RULES = {
    "offer_options_count": "2-3 time options to a prospect",
    "hold_all_offered_slots": True,
    "reminder_after_days": 3,
    "release_hold_after_days": 3,
    "re_remind_on_release": True,
    "weekly_cleanup": "By end of every Friday, ideally no holds remain for the following week.",
    "notes": "Open holds clog the calendar — stay on top of these.",
}

RESCHEDULE_RULES = {
    "priority_over_new": True,
    "options_to_offer": 2,
    "hold_rescheduled_slots": True,
    "reply_window_days": 1,
    "release_after_no_reply": True,
}

# ─────────────────────────────────────────────────────────────
# TIME ZONE RULES
# ─────────────────────────────────────────────────────────────

TIMEZONE_RULES = {
    "internal_calendar": "MT (Mountain Time)",
    "external_emails": (
        "Always quote the recipient's local time zone FIRST, with MT in parentheses. "
        "Example: 'Kory is available Thursday at 4:00 PM Eastern (2:00 PM MT).' "
        "This makes it look like we're putting them first."
    ),
}

# ─────────────────────────────────────────────────────────────
# EMAIL TONE AND FORMAT
# ─────────────────────────────────────────────────────────────

EMAIL_RULES = {
    "sign_off": "Let's Win",
    "never_use": ["Best", "Warmly", "Regards"],
    "tone": "Match the tone of Kory's sent emails or Mia's drafts.",
    "reply_wait_window_minutes": 30,
    "format_note": "Quote contact's time zone first with MT in parentheses.",
    "never_mention": [
        "YPO",
        "YPOer",
        "Young Presidents' Organization",
        "fellow YPO member",
        "YPO connection",
    ],
    "never_mention_note": "Never reference YPO or YPO membership in any outgoing email draft, even if the sender mentions it.",
}

# ─────────────────────────────────────────────────────────────
# URGENT MEETING EXCEPTION
# ─────────────────────────────────────────────────────────────

URGENT_EXCEPTION = {
    "description": "If something critical comes up within a week (new client, time-sensitive deal), agent can ASK Kory to move other meetings.",
    "can_never_move": [
        "YPO Forum",
        "Board meetings",
        "Doug (executive coach)",
        "Trainer workouts",
        "Capital Demolition",
        "HRT appointments",
    ],
}

# ─────────────────────────────────────────────────────────────
# HARD NO DEFAULTS (quick reference)
# ─────────────────────────────────────────────────────────────

HARD_NO_DEFAULTS = [
    "Lunch meetings (work through lunch by default)",
    "Anything scheduled after a happy hour",
    "Anything past 6 PM unless it is a planned dinner",
    "More than 2 happy hours per week",
    "More than 1 dinner per week",
    "Weekend meetings (default no, rare exception only)",
    "Routinely overriding WOB blocks",
]

# ─────────────────────────────────────────────────────────────
# HARD YES DEFAULTS (quick reference)
# ─────────────────────────────────────────────────────────────

HARD_YES_DEFAULTS = [
    "6-7 AM Tue/Thu for East Coast contacts (occasionally)",
    "Phone calls during drive time (any drive 15+ min)",
    "Cherry Creek for all in-person Denver meetings",
    "8:30 or 9:00 AM for coffee meetings",
    "3:30 or 4:00 PM for happy hour meetings",
    "Virtual format by default unless requester specifies otherwise",
]

# ─────────────────────────────────────────────────────────────
# APPROVAL REQUIREMENTS (Phase 1)
# ─────────────────────────────────────────────────────────────

APPROVAL_RULES = {
    "phase": 1,
    "kory_approves_all": True,
    "no_autonomous_sends": True,
    "no_autonomous_bookings": True,
    "description": (
        "In Phase 1, EVERY proposed action — email draft, calendar hold, calendar booking, "
        "reschedule offer — must be reviewed and approved by Kory before execution. "
        "The agent proposes only. Kory decides."
    ),
}
