import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from maildesk.agent_deadline import resolve_deadline


class DeadlineTests(unittest.TestCase):
    # Tuesday in China, still Monday in New York.
    sent = datetime(2026, 11, 24, 1, tzinfo=timezone(timedelta(hours=8))).timestamp()

    def parsed(self, day, clock="", zone="北京时间", **kwargs):
        return resolve_deadline(day, clock, zone, self.sent, **kwargs)

    def test_chinese_relative_and_written_calendar(self):
        for day, date in [("下周三", "2026-12-02"), ("本周三", "2026-11-25"),
                          ("十一月二十六日", "2026-11-26"), ("二〇二六年十一月二十六号", "2026-11-26")]:
            text = f"请在{day}下午两点半（北京时间）之前提交稿件。"
            result = self.parsed(day, "下午两点半", evidence=text)
            self.assertEqual(result.isoformat(), f"{date}T14:30:00+08:00")
        cases = [("上午九点一刻", "09:15"), ("下午两点四十五分", "14:45"),
                 ("中午十二时", "12:00"), ("下午2：30", "14:30"), ("17:30", "17:30")]
        for raw, expected in cases:
            result = self.parsed("明天", raw, evidence=f"请在明天{raw}（北京时间）前回复。")
            self.assertEqual(result.strftime("%H:%M"), expected)

    def test_english_weekdays_and_ordinal_calendar_dates(self):
        for day, expected in [("on Wed.", 25), ("Thursday", 26), ("this Friday", 27),
                              ("next Tue", 1), ("Nov. 26th, 2026", 26), ("26 November 2026", 26)]:
            result = self.parsed(day, "3:15 p.m.", "UTC", evidence=f"Submit by {day} at 3:15 p.m. UTC.")
            self.assertEqual((result.day, result.hour, result.minute), (expected, 15, 15))
        self.assertEqual(self.parsed("tomorrow", "12 AM", "EST").date().isoformat(), "2026-11-24")

    def test_date_only_is_the_displayed_end_of_day_suggestion(self):
        due = self.parsed("十一月二十六日", "", "UTC+05:30", evidence="请于十一月二十六日提交（UTC+05:30）。")
        self.assertEqual(due.isoformat(), "2026-11-26T23:59:00+05:30")

    def test_no_year_without_sent_date_and_invalid_inputs(self):
        for day in ("十一月二十六日", "明天"):
            with self.assertRaisesRegex(ValueError, "来信日期"):
                resolve_deadline(day, "17:00", "UTC", 0)
        for day, clock in [("2026-02-29", ""), ("2026-11-02", "25:00"), ("本周三", "11:90")]:
            with self.assertRaises(ValueError):
                self.parsed(day, clock)

    def test_evidence_cannot_drop_a_clock_zone_or_meridiem(self):
        for clock, zone, evidence in [
            ("", "", "请在周三下午两点半之前提交。"),
            ("3:00", "", "Submit on Wednesday, 3:00 PM."),
            ("17:00", "", "Submit on Wednesday at 17:00 (CST)."),
            ("17:00", "", "Submit on Wednesday at 17:00 ET."),
            ("17:00", "", "Submit on Wednesday at 17:00 XYZ."),
            ("17:00", "", "Submit on Wednesday at 17:00 -0500."),
            ("17:00", "UTC", "Submit on Wednesday at 17:00 UTC+8."),
        ]:
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                self.parsed("Wednesday", clock, zone, evidence=evidence)

    def test_unknown_and_ambiguous_zones_need_clarification(self):
        for zone in ("CST", "IST", "XYZ", "UTC+14:30", "当地时间", "America/Not_A_Zone"):
            with self.subTest(zone=zone), self.assertRaisesRegex(ValueError, "时区"):
                self.parsed("Wednesday", "11:00", zone)

    def test_clipped_quotes_cannot_drop_adjacent_clock_or_zone(self):
        for quote, full, clock in [
            ("Submit by Wednesday at 3 PM", "Submit by Wednesday at 3 PM (CST).", "3 PM"),
            ("请在下周三", "请在下周三下午两点半前提交。", ""),
            ("请于明天 17:00 提交", "北京时间，请于明天 17:00 提交。", "17:00"),
        ]:
            with self.subTest(full=full), self.assertRaises(ValueError):
                self.parsed("Wednesday", clock, "", evidence=quote, source_text=full)
        due = self.parsed("Wednesday", "", "", evidence="Submit by Wednesday.",
                          source_text="Submit by Wednesday. A separate meeting is at 3 PM ET.")
        self.assertEqual(due.time().isoformat(), "23:59:00")

    def test_no_iana_database_keeps_fixed_offsets_working(self):
        with patch("maildesk.agent_deadline.ZoneInfo", side_effect=ZoneInfoNotFoundError("absent")):
            self.assertEqual(self.parsed("Wednesday").utcoffset(), timedelta(hours=8))
            self.assertEqual(self.parsed("Wednesday", zone="UTC").utcoffset(), timedelta())
            with self.assertRaisesRegex(ValueError, "时区数据"):
                self.parsed("Wednesday", zone="ET")

    def test_dst_fold_and_gap_for_named_region(self):
        try:
            ZoneInfo("America/New_York")
        except ZoneInfoNotFoundError:
            self.skipTest("No IANA zones installed on this system")
        for day, clock in [("2026-03-08", "2:30 AM"), ("2026-11-01", "1:30 AM")]:
            with self.assertRaisesRegex(ValueError, "夏令时"):
                self.parsed(day, clock, "ET")
        daylight = self.parsed("2026-11-01", "1:30 AM", "EDT")
        standard = self.parsed("2026-11-01", "1:30 AM", "EST")
        self.assertEqual(standard.timestamp() - daylight.timestamp(), 3600)

    @unittest.skipUnless(hasattr(time, "tzset"), "System timezone is not configurable here")
    def test_dst_fold_and_gap_for_system_local_time(self):
        try:
            with patch.dict(os.environ, {"TZ": "America/New_York"}):
                time.tzset()
                for day, clock in [("2026-03-08", "02:30"), ("2026-11-01", "01:30")]:
                    with self.assertRaisesRegex(ValueError, "夏令时"):
                        self.parsed(day, clock, "")
                self.assertEqual(self.parsed("2026-12-01", "12:00", "").utcoffset(), timedelta(hours=-5))
        finally:
            time.tzset()
