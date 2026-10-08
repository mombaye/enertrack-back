import io
from datetime import datetime

import openpyxl
from django.contrib.auth import get_user_model
from django.test import TestCase

from core.models import Site
from core.site_proposition_import import import_proposition, is_proposition_file
from financial.models import SiteMonthlyLoad


def proposition_file(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Export"
    ws.append([len(rows)])
    ws.append(["Site_ID", "Site_Name", "Typologie facturée", " Typologies cible Contractuel ", "New typo",
               "Typologie réelle", "Configuration", "Bureau", "Date mise en service", "Zone", "Month",
               "Contractual_Load", "Average_Load"])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class SitePropositionImportTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create(username="ops")
        Site.objects.create(site_id="DKR_0001", name="ANCIEN", contract_number="C-1", site_type="OUTDOOR")

    def test_creates_updates_without_erasing_and_loads_month(self):
        month = datetime(2026, 9, 1)
        data = proposition_file([
            ["DKR_0001", "NOUVEAU", "A_Ax_GG", None, None, "A_Ax_GG S0", "Indoor", "non", None, "DAKAR", month, 4000, 4500],
            ["DkR_0002", "SITE2", None, None, None, None, "Outdoor", "non", None, "DAKAR", month, 3000, 3500],
        ])
        self.assertTrue(is_proposition_file(data))
        res = import_proposition(data, self.user)
        self.assertEqual((res["created"], res["updated"], res["errors_count"]), (1, 1, 0))
        s1 = Site.objects.get(site_id="DKR_0001")
        self.assertEqual((s1.name, s1.installed_typology, s1.site_type), ("NOUVEAU", "A_Ax_GG S0", "INDOOR"))
        self.assertEqual(s1.contract_number, "C-1")  # jamais effacé par une cellule absente
        s2 = Site.objects.get(site_id="DKR_0002")  # casse normalisée
        self.assertEqual((s2.zone, s2.site_type), ("DKR", "OUTDOOR"))
        self.assertEqual(SiteMonthlyLoad.objects.get(site=s2, year=2026, month=9).load_w, 3500)
        again = import_proposition(data, self.user)
        self.assertEqual((again["created"], again["updated"], again["unchanged"]), (0, 0, 2))
