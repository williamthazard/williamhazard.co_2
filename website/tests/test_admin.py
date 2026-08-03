from django.test import TestCase
from django.contrib.admin.sites import AdminSite
from website.models import Page, LogEntry, Webmention
from website.admin import PageAdmin, LogEntryAdmin, WebmentionAdmin

class AdminTestCase(TestCase):
    def setUp(self):
        self.site = AdminSite()

    def test_admin_registration(self):
        page_admin = PageAdmin(Page, self.site)
        log_admin = LogEntryAdmin(LogEntry, self.site)
        wm_admin = WebmentionAdmin(Webmention, self.site)
        self.assertIsNotNone(page_admin)
        self.assertIsNotNone(log_admin)
        self.assertIsNotNone(wm_admin)
