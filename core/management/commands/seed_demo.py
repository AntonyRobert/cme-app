"""
Fill an empty development database with made-up data, so the admin has
something in it. Every person and address here is fictional.

    python manage.py seed_demo

Each scenario below is there to show one behaviour; the comments say which.
"""
import datetime
import os
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from attendance.models import AttendanceRecord
from attendance.services import log_manual_row, store_upload, supersede_rows
from audit.log import record
from credits.models import CreditAdjustment, EvaluationResponse, EvaluationSubmission
from credits.windows import request_reopening
from people.models import AllowedDomain, Person, PersonEmail
from rounds.coi import declare
from rounds.models import (
    LearningObjective,
    RoundsEvent,
    Session,
    SessionPresenter,
    coi_questions,
)

Source = AttendanceRecord.Source
Match = AttendanceRecord.MatchMethod
Role = Person.Role

SEED_USERNAME = "seed-script"


class Command(BaseCommand):
    help = "Create fictional demo data in an empty development database."

    def add_arguments(self, parser):
        parser.add_argument(
            "--allow-non-debug",
            action="store_true",
            help="Run even though DEBUG is off (the test suite needs this).",
        )

    def handle(self, *args, **options):
        if os.environ.get("DJANGO_SETTINGS_MODULE", "").endswith(".prod") or not (
            settings.DEBUG or options.get("allow_non_debug")
        ):
            raise CommandError("seed_demo only runs with development settings.")
        if Person.objects.exists() or RoundsEvent.objects.exists():
            raise CommandError(
                "This database already has people or events. seed_demo only fills an empty one."
            )
        with transaction.atomic():
            self.seed()
        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {Person.objects.count()} people, {RoundsEvent.objects.count()} events, "
                f"{Session.objects.count()} sessions, {AttendanceRecord.objects.count()} "
                f"attendance rows ({AttendanceRecord.objects.unmatched().count()} unmatched), "
                f"{EvaluationSubmission.objects.count()} evaluations."
            )
        )

    # --- Helpers -------------------------------------------------------------

    def person(
        self, given, family, role, email, credential="", licence=None, jurisdiction=None,
        affiliation="", employer="",
    ):
        person = Person.objects.create(
            given_name=given,
            family_name=family,
            role=role,
            credential=credential,
            licence_number=licence,
            licence_jurisdiction=jurisdiction,
            affiliation=affiliation,
            employer=employer,
        )
        if email:
            PersonEmail.objects.create(person=person, email=email, is_primary=True)
        return person

    def event(self, days_ago, status, ran_over_minutes=0):
        """Noon to three, Montreal time: three one-hour sessions, 3.00 credits."""
        zone = ZoneInfo(settings.TIME_ZONE)
        day = timezone.localdate() - datetime.timedelta(days=days_ago)
        start = datetime.datetime.combine(day, datetime.time(12, 0), tzinfo=zone)
        return RoundsEvent.objects.create(
            date=day,
            start_at=start,
            end_at=start + datetime.timedelta(minutes=180 + ran_over_minutes),
            status=status,
            accredited_credits=Decimal("3.00"),
            teams_join_url="https://teams.example/l/meetup-join/demo",
            teams_meeting_title="Health Informatics Rounds",
        )

    def session(self, event, position, title, presenters, objectives, ran_over_minutes=0):
        """Session `position` fills its hour: 12:00, 13:00 or 14:00."""
        start = event.start_at + datetime.timedelta(hours=position - 1)
        session = Session.objects.create(
            event=event,
            position=position,
            title=title,
            start_at=start,
            end_at=start + datetime.timedelta(minutes=60 + ran_over_minutes),
            published_blurb=f"A one-hour talk: {title}.",
            submitted_at=event.start_at - datetime.timedelta(days=5),
        )
        for order, presenter in enumerate(presenters, start=1):
            SessionPresenter.objects.create(session=session, person=presenter, position=order)
        for order, text in enumerate(objectives, start=1):
            LearningObjective.objects.create(session=session, position=order, text=text)
        return session

    def declare(self, person, conflicts=None):
        """A declaration made 40 days ago: no to everything, or yes where `conflicts` says."""
        answers = {key: (False, "") for key, _ in coi_questions(settings.COI_CURRENT_VERSION)}
        for key, details in (conflicts or {}).items():
            answers[key] = (True, details)
        return declare(person, answers, declared_at=timezone.now() - datetime.timedelta(days=40))

    def teams(self, event, upload, name, start, end, person=None, email=None, role="Attendee"):
        """A Teams join from `start` to `end` minutes after noon."""
        return AttendanceRecord.objects.create(
            source=Source.TEAMS_UPLOAD,
            upload=upload,
            parser_version="seed",
            event=event,
            raw_display_name=name,
            raw_email=email,
            raw_participant_role=role,
            join_at=event.start_at + datetime.timedelta(minutes=start),
            leave_at=event.start_at + datetime.timedelta(minutes=end),
            person=person,
            match_method=Match.EMAIL_EXACT if person else Match.UNMATCHED,
            matched_at=timezone.now() if person else None,
            created_by=self.staff,
        )

    def by_hand(self, **fields):
        row = AttendanceRecord.objects.create(created_by=self.staff, **fields)
        log_manual_row(row, user=self.staff)
        return row

    def evaluate(self, person, session, minutes=20, complete=True, comment=None):
        submission = EvaluationSubmission.objects.create(
            person=person,
            session=session,
            submitted_at=session.event.end_at + datetime.timedelta(hours=3),
            self_reported_session_minutes=minutes,
            attestation=True,
            is_complete=complete,
        )
        if complete:
            for objective in session.objectives.all():
                EvaluationResponse.objects.create(
                    submission=submission, objective=objective, question_key="objective_met", rating=4
                )
            EvaluationResponse.objects.create(
                submission=submission, question_key="overall", rating=5, free_text=comment
            )
        return submission

    def upload(self, event, label):
        content = (
            "SYNTHETIC SEED DATA - not a real Teams export\r\n"
            f"Meeting title,Health Informatics Rounds ({label})\r\n"
            f"Start time,{event.start_at.isoformat()}\r\n"
        ).encode()
        upload = store_upload(
            event, SimpleUploadedFile(f"Rounds {event.date} - Attendance report.csv", content),
            user=self.staff,
        )
        upload.parsed_at = timezone.now()
        upload.parser_version = "seed"
        upload.save()
        return upload

    # --- The data ------------------------------------------------------------

    def seed(self):
        self.staff, _ = get_user_model().objects.get_or_create(
            username=SEED_USERNAME, defaults={"is_active": False, "is_staff": False}
        )
        self.staff.set_unusable_password()
        self.staff.save()

        AllowedDomain.objects.create(domain="mcgill.ca", note="University")
        AllowedDomain.objects.create(domain="muhc.mcgill.ca", note="MUHC")
        AllowedDomain.objects.create(
            domain="example.org", note="Fictional addresses used by seed_demo. Remove in production."
        )
        AllowedDomain.objects.create(
            domain="example.com", auto_admit=False, note="Demo: a domain that goes to review"
        )

        p = self.person
        # A leading zero: printed as entered, matched without it.
        tremblay = p("Marie", "Tremblay", Role.PHYSICIAN, "marie.tremblay@example.org", "MD", "01234", "CMQ")
        cote = p("Jean-François", "Côté", Role.PHYSICIAN, "jf.cote@example.org", "MD", "23456", "CMQ")
        # Same number as Côté in another jurisdiction: allowed.
        haddad = p("Amira", "Haddad", Role.PHYSICIAN, "amira.haddad@example.org", "MD", "23456", "CPSO")
        gagnon = p(
            "Marc", "Gagnon", Role.PHYSICIAN, "marc.gagnon@example.org", "MD", "34567", "CMQ",
            affiliation="Department of Medicine, Example University",
            employer="Example General Hospital",
        )
        morin = p("Olivier", "Morin", Role.PHYSICIAN, "olivier.morin@example.org", "MD", "45 678", "CMQ")
        nguyen = p("Sophie", "Nguyen", Role.NURSE, "sophie.nguyen@example.org", "RN", "987654", "OIIQ")
        lavoie = p("Chantal", "Lavoie", Role.NURSE, "chantal.lavoie@example.org", "RN")
        okafor = p("David", "Okafor", Role.PHARMACIST, "david.okafor@example.org", "PharmD", "4521", "OPQ")
        bouchard = p("Léa", "Bouchard", Role.TRAINEE, "lea.bouchard@example.org", "MD")
        roy = p("Samuel", "Roy", Role.STUDENT, "samuel.roy@example.org")
        sharma = p(
            "Priya", "Sharma", Role.OTHER, "priya.sharma@example.org", "PhD",
            affiliation="School of Information Studies, Example University; Example Health Informatics Lab",
        )
        # The same Marie Tremblay, created again under her hospital address.
        # Filter People by "Possible duplicates" and merge the two.
        tremblay_dup = p("Marie", "Tremblay", Role.PHYSICIAN, "m.tremblay@example.com", "MD")

        # Conflict-of-interest declarations. Haddad has none on file.
        self.declare(
            gagnon,
            {
                "consulting": "Advisory board member, Acme Patient Monitoring.",
                "speaker_fees": "Honoraria from Acme Patient Monitoring, 2025.",
            },
        )
        for presenter in (sharma, cote, okafor):
            self.declare(presenter)

        first = self.event(days_ago=28, status=RoundsEvent.Status.CLOSED)
        second = self.event(days_ago=14, status=RoundsEvent.Status.HELD, ran_over_minutes=5)

        a1 = self.session(
            first, 1, "Sepsis alerts: what the audit showed", [gagnon],
            ["Describe how the sepsis alert is triggered", "Interpret the alert audit results"],
        )
        a2 = self.session(
            first, 2, "FHIR at the bedside", [sharma],
            ["Explain what a FHIR resource is", "Identify one bedside use of FHIR data"],
        )
        a3 = self.session(
            first, 3, "Order sets and alert fatigue", [cote],
            ["List two causes of alert fatigue", "Propose one change to an order set"],
        )
        b1 = self.session(
            second, 1, "Clinical decision support for anticoagulation", [okafor],
            ["Describe the dosing support rules", "Recognize when to override them"],
        )
        b2 = self.session(
            second, 2, "Dashboards nobody opens", [sharma, gagnon],  # co-presenters
            ["Explain why dashboards go unused", "Choose one metric worth showing"],
        )
        # The last talk ran five minutes over; its end time says so.
        b3 = self.session(
            second, 3, "Ambient scribes: early lessons", [haddad],
            ["Summarize the pilot results", "Discuss consent for ambient recording"],
            ran_over_minutes=5,
        )

        # ===== First event: 12:00, 13:00 and 14:00, an hour each =====
        up1 = self.upload(first, "first demo event")
        t = self.teams

        # Whole event on one connection, evaluated every session: 3.00.
        t(first, up1, "Tremblay, Marie", -2, 181, tremblay, "marie.tremblay@example.org")
        # Three rejoins. Session 1 comes to 54 minutes, one short of counting
        # in full; only session 1 is evaluated: 0.75.
        for start_min, end_min in ((0, 42), (48, 120), (123, 180)):
            t(first, up1, "Côté, Jean-François", start_min, end_min, cote, "jf.cote@example.org", "Presenter")
        # Laptop and phone at the same time. Counted once. Two sessions evaluated: 2.00.
        t(first, up1, "Haddad, Amira", 0, 180, haddad, "amira.haddad@example.org")
        t(first, up1, "Amira (phone)", 60, 135, haddad, "amira.haddad@example.org")
        # In the lobby from 11:40, left at 13:33. Session 1 in full (the five
        # minutes before noon count, the rest of the wait does not): 1.00.
        t(first, up1, "Nguyen, Sophie", -20, 93, nguyen, "sophie.nguyen@example.org")
        # Joined a quarter past. His only evaluation is incomplete: no computed credit.
        t(first, up1, "Okafor, David", 15, 180, okafor, "david.okafor@example.org")
        t(first, up1, "Bouchard, Léa", 0, 180, bouchard, "lea.bouchard@example.org")
        t(first, up1, "Gagnon, Marc", 0, 180, gagnon, "marc.gagnon@example.org", "Presenter")

        # A meeting-room device with two people behind it.
        room = t(first, up1, "Conference Room B", 3, 177)
        self.by_hand(
            source=Source.ROOM_ROSTER, event=first, attributed_to=room, person=lavoie,
            reason="On the Room B sign-in sheet",
        )
        self.by_hand(
            source=Source.ROOM_ROSTER, event=first, attributed_to=room, person=roy,
            join_at=first.start_at + datetime.timedelta(minutes=75), leave_at=room.leave_at,
            reason="On the Room B sign-in sheet; arrived 13:15",
        )

        # Teams only saw her laptop for two short stretches while she presented
        # the second session from the podium PC. One manual row replaces both.
        short_rows = [
            t(first, up1, "Sharma, Priya", 60, 70, sharma, "priya.sharma@example.org", "Presenter"),
            t(first, up1, "Sharma, Priya", 110, 120, sharma, "priya.sharma@example.org", "Presenter"),
        ]
        correction = self.by_hand(
            source=Source.MANUAL, event=first, session=a2, person=sharma,
            duration_seconds=55 * 60,
            reason="Presented from the podium PC; Teams only captured her laptop. Chair confirms.",
        )
        supersede_rows(short_rows, correction, user=self.staff)
        t(first, up1, "Sharma, Priya", 120, 180, sharma, "priya.sharma@example.org")

        # The review queue: rows nobody could be matched to.
        t(first, up1, "iPhone de Marc", 6, 174)
        t(first, up1, "S. External", 0, 180, email="s.external@elsewhere.example")

        ev = self.evaluate
        for session in (a1, a2, a3):
            ev(tremblay, session, minutes=60, comment="Very practical.")
            # Morin has no attendance row at all: credit rests on his self-report
            # and is flagged for review.
            ev(morin, session, minutes=60)
        ev(cote, a1, minutes=55)
        ev(haddad, a1, minutes=60)
        ev(haddad, a2, minutes=60)
        ev(nguyen, a1, minutes=60)
        ev(okafor, a2, minutes=60, complete=False)
        ev(bouchard, a3, minutes=60)
        ev(lavoie, a1, minutes=60)
        ev(sharma, a3, minutes=60)
        ev(gagnon, a2, minutes=60)

        # The hours are right and the credit still isn't: that is what an adjustment is for.
        adjustment = CreditAdjustment.objects.create(
            person=okafor, event=first, delta_credits=Decimal("0.75"), created_by=self.staff,
            reason="Evaluation form would not submit on his device; chair approved the credit.",
        )
        record(
            "credit.adjusted", adjustment, user=self.staff,
            metadata={"delta_credits": adjustment.delta_credits, "reason": adjustment.reason},
        )

        # Bouchard attended all three but only evaluated the third; she asked
        # for another week on the first.
        request_reopening(bouchard, a1, reason="Was on call the week after")

        # ===== Second event: the third talk ran to 15:05 =====
        up2 = self.upload(second, "second demo event")

        # Her Teams rows landed on the duplicate record, her evaluation on the
        # main one. Until the two are merged, the duplicate has minutes but no
        # evaluation, and the main record's credit rests on her self-report
        # (flagged for review).
        t(second, up2, "Marie Tremblay", 0, 185, tremblay_dup, "m.tremblay@example.com")
        ev(tremblay, b1, minutes=60, comment="Would like the dosing table as a handout.")

        # Stayed for the overrun. Session 3 is 65 minutes long now.
        t(second, up2, "Côté, Jean-François", 0, 190, cote, "jf.cote@example.org")
        t(second, up2, "Haddad, Amira", 0, 190, haddad, "amira.haddad@example.org", "Presenter")
        t(second, up2, "Sharma, Priya", 0, 190, sharma, "priya.sharma@example.org", "Presenter")
        t(second, up2, "Gagnon, Marc", 0, 190, gagnon, "marc.gagnon@example.org", "Presenter")
        t(second, up2, "Nguyen, Sophie", 0, 170, nguyen, "sophie.nguyen@example.org")

        # Laptop died twenty minutes into the first talk; phoned in for the rest
        # of it. An hours-only manual row for that session adds on top.
        t(second, up2, "Okafor, David", 0, 20, okafor, "david.okafor@example.org", "Presenter")
        self.by_hand(
            source=Source.MANUAL, event=second, session=b1, person=okafor,
            duration_seconds=40 * 60,
            reason="Laptop died at 12:20; phoned in for the rest of the talk. Chair confirms.",
        )

        # Three rejoins from an address not on file. Match one in the review
        # queue and the other two follow.
        for start_min, end_min in ((0, 66), (72, 141), (147, 190)):
            t(second, up2, "Lea B.", start_min, end_min, email="Lea.Bouchard@hospital.example")

        # Recorded 30 minutes of the first talk, claims 60. Flagged because
        # the claim is more than 15 minutes above the record.
        t(second, up2, "Lavoie, Chantal", 0, 30, lavoie, "chantal.lavoie@example.org")
        ev(lavoie, b1, minutes=60)

        ev(cote, b1, minutes=60)
        ev(haddad, b2, minutes=60)
        ev(okafor, b1, minutes=60)
        ev(nguyen, b2, minutes=60)
        ev(sharma, b1, minutes=60)
        ev(gagnon, b3, minutes=65)
