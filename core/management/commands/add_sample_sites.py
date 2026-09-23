from django.core.management.base import BaseCommand
from core.models import Site


SAMPLE_SITES = [
    # Dakar
    {"site_id": "DKR001", "name": "Dakar Centre Tour A", "zone": "DKR", "site_type": "OUTDOOR", "scope_status": "IN_SCOPE", "invoice_payment": "Sonatel", "batch_operational": "BATCH_01", "billing_typology": "A_Ax_GG", "installed_typology": "A_Ax_GG", "load_band": "MEDIUM", "grid_fee": True, "country": "sen"},
    {"site_id": "DKR002", "name": "Dakar Plateau Indoor", "zone": "DKR", "site_type": "INDOOR", "scope_status": "IN_SCOPE", "invoice_payment": "Sonatel", "batch_operational": "BATCH_01", "billing_typology": "D2_GG", "installed_typology": "D2_GG", "load_band": "LOW", "grid_fee": False, "country": "sen"},
    {"site_id": "DKR003", "name": "Dakar Almadies", "zone": "DKR", "site_type": "OUTDOOR", "scope_status": "IN_SCOPE", "invoice_payment": "Aktivco", "batch_operational": "BATCH_02", "billing_typology": "A_Ax_GG S1", "installed_typology": "A_Ax_GG S1", "load_band": "HIGH", "grid_fee": True, "country": "sen"},
    {"site_id": "DKR004", "name": "Dakar Parcelles", "zone": "DKR", "site_type": "OUTDOOR", "scope_status": "IN_SCOPE", "invoice_payment": "Sonatel", "batch_operational": "BATCH_01", "billing_typology": "C4_GG", "installed_typology": "C4_GG", "load_band": "MEDIUM", "grid_fee": False, "country": "sen"},
    {"site_id": "DKR005", "name": "Dakar Yoff", "zone": "DKR", "site_type": "OUTDOOR", "scope_status": "OUT_OF_SCOPE", "invoice_payment": "Sonatel", "batch_operational": "BATCH_03", "billing_typology": "A_Ax_GG GE", "installed_typology": "A_Ax_GG GE", "load_band": "LOW", "grid_fee": True, "country": "sen"},
    {"site_id": "DKR006", "name": "Dakar Thiaroye", "zone": "DKR", "site_type": "INDOOR", "scope_status": "IN_SCOPE", "invoice_payment": "Aktivco", "batch_operational": "BATCH_02", "billing_typology": "D2_GG", "installed_typology": "D2_GG", "load_band": "MEDIUM", "grid_fee": False, "country": "sen"},
    {"site_id": "DKR007", "name": "Dakar Pikine", "zone": "DKR", "site_type": "OUTDOOR", "scope_status": "IN_SCOPE", "invoice_payment": "Sonatel", "batch_operational": "BATCH_01", "billing_typology": "A_Ax_GG S2", "installed_typology": "A_Ax_GG S2", "load_band": "HIGH", "grid_fee": True, "country": "sen"},
    # Thiès
    {"site_id": "THS001", "name": "Thiès Centre", "zone": "THS", "site_type": "OUTDOOR", "scope_status": "IN_SCOPE", "invoice_payment": "Sonatel", "batch_operational": "BATCH_02", "billing_typology": "A_Ax_GG", "installed_typology": "A_Ax_GG", "load_band": "MEDIUM", "grid_fee": True, "country": "sen"},
    {"site_id": "THS002", "name": "Thiès Mbour", "zone": "THS", "site_type": "OUTDOOR", "scope_status": "IN_SCOPE", "invoice_payment": "Aktivco", "batch_operational": "BATCH_03", "billing_typology": "C4_GG", "installed_typology": "C4_GG", "load_band": "LOW", "grid_fee": False, "country": "sen"},
    {"site_id": "THS003", "name": "Thiès Saly", "zone": "THS", "site_type": "INDOOR", "scope_status": "IN_SCOPE", "invoice_payment": "Sonatel", "batch_operational": "BATCH_01", "billing_typology": "D2_GG", "installed_typology": "D2_GG", "load_band": "MEDIUM", "grid_fee": True, "country": "sen"},
    {"site_id": "THS004", "name": "Thiès Tivaouane", "zone": "THS", "site_type": "OUTDOOR", "scope_status": "OUT_OF_SCOPE", "invoice_payment": "Sonatel", "batch_operational": "BATCH_02", "billing_typology": "A_Ax_GG S0", "installed_typology": "A_Ax_GG S0", "load_band": "LOW", "grid_fee": False, "country": "sen"},
    # Diourbel
    {"site_id": "DBL001", "name": "Diourbel Centre", "zone": "DBL", "site_type": "OUTDOOR", "scope_status": "IN_SCOPE", "invoice_payment": "Sonatel", "batch_operational": "BATCH_01", "billing_typology": "A_Ax_GG", "installed_typology": "A_Ax_GG", "load_band": "LOW", "grid_fee": True, "country": "sen"},
    {"site_id": "DBL002", "name": "Touba Principal", "zone": "DBL", "site_type": "OUTDOOR", "scope_status": "IN_SCOPE", "invoice_payment": "Aktivco", "batch_operational": "BATCH_02", "billing_typology": "A_Ax_GG GE", "installed_typology": "A_Ax_GG GE", "load_band": "HIGH", "grid_fee": True, "country": "sen"},
    {"site_id": "DBL003", "name": "Mbacké Site Nord", "zone": "DBL", "site_type": "OUTDOOR", "scope_status": "IN_SCOPE", "invoice_payment": "Sonatel", "batch_operational": "BATCH_03", "billing_typology": "D2_GG", "installed_typology": "D2_GG", "load_band": "MEDIUM", "grid_fee": False, "country": "sen"},
    # Saint-Louis
    {"site_id": "STL001", "name": "Saint-Louis Centre", "zone": "STL", "site_type": "OUTDOOR", "scope_status": "IN_SCOPE", "invoice_payment": "Sonatel", "batch_operational": "BATCH_01", "billing_typology": "A_Ax_GG", "installed_typology": "A_Ax_GG", "load_band": "MEDIUM", "grid_fee": True, "country": "sen"},
    {"site_id": "STL002", "name": "Saint-Louis Langue de Barbarie", "zone": "STL", "site_type": "INDOOR", "scope_status": "IN_SCOPE", "invoice_payment": "Aktivco", "batch_operational": "BATCH_02", "billing_typology": "C4_GG", "installed_typology": "C4_GG", "load_band": "LOW", "grid_fee": False, "country": "sen"},
    # Ziguinchor
    {"site_id": "ZIG001", "name": "Ziguinchor Plateau", "zone": "ZIG", "site_type": "OUTDOOR", "scope_status": "IN_SCOPE", "invoice_payment": "Sonatel", "batch_operational": "BATCH_01", "billing_typology": "A_Ax_GG S3", "installed_typology": "A_Ax_GG S3", "load_band": "MEDIUM", "grid_fee": True, "country": "sen"},
    {"site_id": "ZIG002", "name": "Ziguinchor Bignona", "zone": "ZIG", "site_type": "OUTDOOR", "scope_status": "OUT_OF_SCOPE", "invoice_payment": "Aktivco", "batch_operational": "BATCH_03", "billing_typology": "D2_GG", "installed_typology": "D2_GG", "load_band": "LOW", "grid_fee": False, "country": "sen"},
    # Kaolack
    {"site_id": "KLD001", "name": "Kaolack Centre", "zone": "KLD", "site_type": "OUTDOOR", "scope_status": "IN_SCOPE", "invoice_payment": "Sonatel", "batch_operational": "BATCH_02", "billing_typology": "A_Ax_GG", "installed_typology": "A_Ax_GG", "load_band": "MEDIUM", "grid_fee": True, "country": "sen"},
    {"site_id": "KLD002", "name": "Kaolack Nioro", "zone": "KLD", "site_type": "OUTDOOR", "scope_status": "IN_SCOPE", "invoice_payment": "Aktivco", "batch_operational": "BATCH_01", "billing_typology": "C4_GG", "installed_typology": "C4_GG", "load_band": "LOW", "grid_fee": False, "country": "sen"},
    # Tambacounda
    {"site_id": "TMB001", "name": "Tambacounda Nord", "zone": "TMB", "site_type": "OUTDOOR", "scope_status": "IN_SCOPE", "invoice_payment": "Sonatel", "batch_operational": "BATCH_03", "billing_typology": "A_Ax_GG S1", "installed_typology": "A_Ax_GG S1", "load_band": "LOW", "grid_fee": True, "country": "sen"},
    {"site_id": "TMB002", "name": "Tambacounda Gare", "zone": "TMB", "site_type": "INDOOR", "scope_status": "UNKNOWN", "invoice_payment": "Aktivco", "batch_operational": "BATCH_02", "billing_typology": "D2_GG", "installed_typology": "D2_GG", "load_band": "LOW", "grid_fee": False, "country": "sen"},
    # Kédougou
    {"site_id": "KDG001", "name": "Kédougou Centre", "zone": "KDG", "site_type": "OUTDOOR", "scope_status": "IN_SCOPE", "invoice_payment": "Sonatel", "batch_operational": "BATCH_01", "billing_typology": "A_Ax_GG", "installed_typology": "A_Ax_GG", "load_band": "LOW", "grid_fee": True, "country": "sen"},
    # Kolda
    {"site_id": "KLK001", "name": "Kolda Centre", "zone": "KLK", "site_type": "OUTDOOR", "scope_status": "IN_SCOPE", "invoice_payment": "Aktivco", "batch_operational": "BATCH_03", "billing_typology": "A_Ax_GG GE", "installed_typology": "A_Ax_GG GE", "load_band": "MEDIUM", "grid_fee": True, "country": "sen"},
    {"site_id": "KLK002", "name": "Kolda Vélingara", "zone": "KLK", "site_type": "OUTDOOR", "scope_status": "UNKNOWN", "invoice_payment": "Sonatel", "batch_operational": "BATCH_02", "billing_typology": "D2_GG", "installed_typology": "D2_GG", "load_band": "LOW", "grid_fee": False, "country": "sen"},
    # Matam
    {"site_id": "MBR001", "name": "Matam Centre", "zone": "MBR", "site_type": "OUTDOOR", "scope_status": "IN_SCOPE", "invoice_payment": "Sonatel", "batch_operational": "BATCH_01", "billing_typology": "A_Ax_GG S2", "installed_typology": "A_Ax_GG S2", "load_band": "LOW", "grid_fee": True, "country": "sen"},
    # Bakel
    {"site_id": "BKL001", "name": "Bakel Principal", "zone": "BKL", "site_type": "OUTDOOR", "scope_status": "IN_SCOPE", "invoice_payment": "Aktivco", "batch_operational": "BATCH_02", "billing_typology": "C4_GG", "installed_typology": "C4_GG", "load_band": "LOW", "grid_fee": False, "country": "sen"},
    # Ndian
    {"site_id": "NDM001", "name": "Ndian Centre", "zone": "NDM", "site_type": "OUTDOOR", "scope_status": "IN_SCOPE", "invoice_payment": "Sonatel", "batch_operational": "BATCH_03", "billing_typology": "A_Ax_GG", "installed_typology": "A_Ax_GG", "load_band": "LOW", "grid_fee": True, "country": "sen"},
    # DKR extras
    {"site_id": "DKR008", "name": "Dakar Médina", "zone": "DKR", "site_type": "INDOOR", "scope_status": "IN_SCOPE", "invoice_payment": "Sonatel", "batch_operational": "BATCH_01", "billing_typology": "D2_GG GE", "installed_typology": "D2_GG GE", "load_band": "MEDIUM", "grid_fee": True, "country": "sen"},
    {"site_id": "DKR009", "name": "Dakar Hann", "zone": "DKR", "site_type": "OUTDOOR", "scope_status": "IN_SCOPE", "invoice_payment": "Aktivco", "batch_operational": "BATCH_02", "billing_typology": "A_Ax_GG S0 GE", "installed_typology": "A_Ax_GG S0 GE", "load_band": "HIGH", "grid_fee": True, "country": "sen"},
    {"site_id": "DKR010", "name": "Dakar Rufisque", "zone": "DKR", "site_type": "OUTDOOR", "scope_status": "OUT_OF_SCOPE", "invoice_payment": "Sonatel", "batch_operational": "BATCH_03", "billing_typology": "E1_E2_GG", "installed_typology": "E1_E2_GG", "load_band": "MEDIUM", "grid_fee": False, "country": "sen"},
]


class Command(BaseCommand):
    help = "Add sample sites for testing the admin /admin/sites page"

    def add_arguments(self, parser):
        parser.add_argument("--clear", action="store_true", help="Delete all existing sites before inserting")

    def handle(self, *args, **options):
        if options["clear"]:
            count = Site.objects.count()
            Site.objects.all().delete()
            self.stdout.write(self.style.WARNING(f"Deleted {count} existing sites."))

        created = 0
        skipped = 0
        for data in SAMPLE_SITES:
            _, is_new = Site.objects.get_or_create(site_id=data["site_id"], defaults=data)
            if is_new:
                created += 1
            else:
                skipped += 1

        self.stdout.write(self.style.SUCCESS(
            f"Done: {created} site(s) created, {skipped} already existed."
        ))
