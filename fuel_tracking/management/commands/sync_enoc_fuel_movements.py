from calendar import monthrange
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime, parse_date

from fuel_tracking.models import FuelEnocMovement, FuelEnocSyncRun
from fuel_tracking.services.enoc_mongo_service import fetch_fuel_movements


def to_decimal(value, default=None):
    if value in (None, "", "NA", "N/A", "na", "n/a"):
        return default

    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def to_bool(value):
    return bool(value)


def parse_dt(value):
    if not value:
        return None

    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        dt = parse_datetime(raw)

        if not dt:
            raw = raw.replace("Z", "")
            raw = raw.split("+")[0]
            raw = raw.split(".")[0]

            candidates = [
                (raw[:19], "%Y-%m-%dT%H:%M:%S"),
                (raw[:19], "%Y-%m-%d %H:%M:%S"),
                (raw[:10], "%Y-%m-%d"),
            ]

            for candidate, fmt in candidates:
                try:
                    dt = datetime.strptime(candidate, fmt)
                    break
                except Exception:
                    pass

    if not dt:
        return None

    if timezone.is_naive(dt):
        return timezone.make_aware(dt, timezone=dt_timezone.utc)

    return dt


def deduplicate_payloads(payloads: list[dict]) -> tuple[list[dict], int]:
    """
    Évite l'erreur PostgreSQL :
    ON CONFLICT DO UPDATE command cannot affect row a second time

    Si ENOC renvoie deux fois le même source_id dans le même batch,
    on garde la version la plus récente.
    """
    deduped = {}
    duplicates = 0

    for item in payloads:
        source_system = str(item.get("source_system") or "ENOC").strip()
        source_id = str(item.get("source_id") or "").strip()

        if not source_id:
            continue

        item["source_system"] = source_system
        item["source_id"] = source_id

        key = (source_system, source_id)

        if key in deduped:
            duplicates += 1

        deduped[key] = item

    return list(deduped.values()), duplicates

def parse_d(value):
    if not value:
        return None

    if hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day"):
        return value

    return parse_date(str(value))


def month_to_range(month_value: str) -> tuple[str, str]:
    year, month = [int(x) for x in month_value.split("-")]
    last_day = monthrange(year, month)[1]
    return f"{year}-{month:02d}-01", f"{year}-{month:02d}-{last_day:02d}"


def clean_payload(item: dict) -> dict:
    return {
        "source_system": item.get("source_system") or "ENOC",
        "source_id": str(item.get("source_id") or "").strip(),

        "request_id": item.get("request_id"),
        "request_code": item.get("request_code"),
        "status": item.get("status") or "done",

        "site_id": item.get("site_id"),
        "site_name": item.get("site_name"),
        "zone": item.get("zone"),
        "ville": item.get("ville"),

        "operation_type": item.get("operation_type"),
        "operation_date": parse_dt(item.get("operation_date")),

        "requested_quantity_liters": to_decimal(item.get("requested_quantity_liters"), Decimal("0")),
        "approved_quantity_liters": to_decimal(item.get("approved_quantity_liters"), Decimal("0")),
        "quantity_added_liters": to_decimal(item.get("quantity_added_liters"), Decimal("0")),

        "level_before": to_decimal(item.get("level_before")),
        "level_before_unit": item.get("level_before_unit"),
        "level_after": to_decimal(item.get("level_after")),
        "level_after_unit": item.get("level_after_unit"),

        "hour_meter_before": to_decimal(item.get("hour_meter_before")),
        "hour_meter_after": to_decimal(item.get("hour_meter_after")),

        "monthly_target_liters": to_decimal(item.get("monthly_target_liters"), Decimal("0")),
        "monthly_total_after_liters": to_decimal(item.get("monthly_total_after_liters"), Decimal("0")),
        "target_percent_after": to_decimal(item.get("target_percent_after"), Decimal("0")),
        "target_status": item.get("target_status"),
        "is_target_exceeded": to_bool(item.get("is_target_exceeded")),

        "ge_snapshot": item.get("ge_snapshot") or {},
        "ponction": item.get("ponction"),

        "technician_name": item.get("technician_name"),
        "technician_phone": item.get("technician_phone"),
        "team": item.get("team"),
        "teammate": item.get("teammate"),
        "rm": item.get("rm"),

        "created_by": item.get("created_by"),
        "validated_by": item.get("validated_by"),
        "done_by": item.get("done_by"),

        "created_at_source": parse_dt(item.get("created_at")),
        "validated_at_source": parse_dt(item.get("validated_at")),
        "done_at_source": parse_dt(item.get("done_at")),
        "source_created_at": parse_dt(item.get("source_created_at")),
        "source_updated_at": parse_dt(item.get("source_updated_at")),

        "import_source": item.get("import_source"),
        "import_key": item.get("import_key"),

        "delivery_note_number": item.get("delivery_note_number"),
        "delivery_note_quantity_liters": to_decimal(item.get("delivery_note_quantity_liters")),
        "supplier": item.get("supplier"),
        "gauging_method": item.get("gauging_method"),
        "rms_level_before": to_decimal(item.get("rms_level_before")),
        "rms_level_after": to_decimal(item.get("rms_level_after")),

        "comment": item.get("comment"),
        "raw_payload": item,
    }


class Command(BaseCommand):
    help = "Synchronise les mouvements Fuel ENOC vers EnerTrack"

    def add_arguments(self, parser):
        parser.add_argument("--month", type=str, default=None, help="YYYY-MM")
        parser.add_argument("--start-date", type=str, default=None, help="YYYY-MM-DD")
        parser.add_argument("--end-date", type=str, default=None, help="YYYY-MM-DD")
        parser.add_argument("--updated-since", type=str, default=None)
        parser.add_argument("--site", type=str, default=None)
        parser.add_argument("--zone", type=str, default=None)
        parser.add_argument("--limit", type=int, default=200)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        month = options["month"]
        start_date = options["start_date"]
        end_date = options["end_date"]

        if month:
            start_date, end_date = month_to_range(month)

        updated_since = options["updated_since"]
        site = options["site"]
        zone = options["zone"]
        limit = min(max(options["limit"], 1), 500)
        dry_run = options["dry_run"]

        sync_run = FuelEnocSyncRun.objects.create(
            start_date=parse_d(start_date),
            end_date=parse_d(end_date),
            updated_since=parse_dt(updated_since),
            status=FuelEnocSyncRun.Status.RUNNING,
        )

        self.stdout.write("\n" + "═" * 80)
        self.stdout.write("  SYNC ENOC FUEL → EnerTrack")
        self.stdout.write("═" * 80)
        self.stdout.write(f"  Start date    : {start_date or '-'}")
        self.stdout.write(f"  End date      : {end_date or '-'}")
        self.stdout.write(f"  Updated since : {updated_since or '-'}")
        self.stdout.write(f"  Site          : {site or '-'}")
        self.stdout.write(f"  Zone          : {zone or '-'}")
        self.stdout.write(f"  Limit/page    : {limit}")
        self.stdout.write(f"  Dry run       : {dry_run}")
        self.stdout.write("═" * 80 + "\n")

        try:
            # L'API REST ENOC (port 8002) est bloquée par filtrage IP côté ENOC
            # (erreur 403 "IP non autorisée") — on lit directement leur base
            # MongoDB (fuel_requests + fuel_operations), qui alimente cette
            # même API. Pas de filtrage par date ici (voir docstring du
            # service) : --start-date/--end-date/--site/--zone/--limit sont
            # acceptés pour compatibilité CLI mais actuellement sans effet.
            all_items = fetch_fuel_movements()
            self.stdout.write(f"  {len(all_items)} mouvement(s) lus depuis MongoDB ENOC (fuel_requests + fuel_operations)")

            payloads = []

            for item in all_items:
                clean = clean_payload(item)

                if not clean["source_id"]:
                    continue

                payloads.append(clean)

            raw_payload_count = len(payloads)
            payloads, duplicate_count = deduplicate_payloads(payloads)

            self.stdout.write(f"\n  Total mouvements Mongo reçus   : {raw_payload_count}")
            self.stdout.write(f"  Total mouvements uniques       : {len(payloads)}")

            if duplicate_count:
                self.stdout.write(
                    self.style.WARNING(
                        f"  Doublons ENOC fusionnés        : {duplicate_count}"
                    )
                )

            site_context_count = sum(
                1 for item in payloads
                if (item.get("raw_payload") or {}).get("site_context")
            )

            ge_context_count = sum(
                1 for item in payloads
                if (item.get("raw_payload") or {}).get("ge_context")
            )

            self.stdout.write(f"  Mouvements avec site_context   : {site_context_count}")
            self.stdout.write(f"  Mouvements avec ge_context     : {ge_context_count}")

            if dry_run:
                for item in payloads[:10]:
                    self.stdout.write(
                        f"  {item['operation_date']} | {item['site_id']} | "
                        f"{item['request_code']} | {item['operation_type']} | "
                        f"{item['quantity_added_liters']} L"
                    )

                sync_run.status = FuelEnocSyncRun.Status.SUCCESS
                sync_run.rows_fetched = len(payloads)
                sync_run.finished_at = timezone.now()
                sync_run.save()

                self.stdout.write(self.style.SUCCESS("\n  DRY RUN terminé. Aucune donnée insérée.\n"))
                return

            source_ids = [item["source_id"] for item in payloads]

            existing_keys = set(
                FuelEnocMovement.objects.filter(
                    source_system="ENOC",
                    source_id__in=source_ids,
                ).values_list("source_system", "source_id")
            )

            now = timezone.now()
            objs = []
            created = 0
            updated = 0

            for item in payloads:
                key = (item["source_system"], item["source_id"])

                if key in existing_keys:
                    updated += 1
                else:
                    created += 1

                objs.append(
                    FuelEnocMovement(
                        **item,
                        synced_at=now,
                        updated_at=now,
                    )
                )

            update_fields = [
                "request_id",
                "request_code",
                "status",
                "site_id",
                "site_name",
                "zone",
                "ville",
                "operation_type",
                "operation_date",
                "requested_quantity_liters",
                "approved_quantity_liters",
                "quantity_added_liters",
                "level_before",
                "level_before_unit",
                "level_after",
                "level_after_unit",
                "hour_meter_before",
                "hour_meter_after",
                "monthly_target_liters",
                "monthly_total_after_liters",
                "target_percent_after",
                "target_status",
                "is_target_exceeded",
                "ge_snapshot",
                "ponction",
                "technician_name",
                "technician_phone",
                "team",
                "teammate",
                "rm",
                "created_by",
                "validated_by",
                "done_by",
                "created_at_source",
                "validated_at_source",
                "done_at_source",
                "source_created_at",
                "source_updated_at",
                "import_source",
                "import_key",
                "delivery_note_number",
                "delivery_note_quantity_liters",
                "supplier",
                "gauging_method",
                "rms_level_before",
                "rms_level_after",
                "comment",
                "raw_payload",
                "synced_at",
                "updated_at",
            ]

            with transaction.atomic():
                FuelEnocMovement.objects.bulk_create(
                    objs,
                    batch_size=1000,
                    update_conflicts=True,
                    unique_fields=["source_system", "source_id"],
                    update_fields=update_fields,
                )

            sync_run.status = FuelEnocSyncRun.Status.SUCCESS
            sync_run.rows_fetched = len(payloads)
            sync_run.rows_created = created
            sync_run.rows_updated = updated
            sync_run.finished_at = timezone.now()
            sync_run.save()

            self.stdout.write(self.style.SUCCESS("\n  Synchronisation ENOC terminée."))
            self.stdout.write(f"  Créées       : {created}")
            self.stdout.write(f"  Mises à jour : {updated}\n")

        except Exception as exc:
            sync_run.status = FuelEnocSyncRun.Status.FAILED
            sync_run.error_message = str(exc)
            sync_run.finished_at = timezone.now()
            sync_run.save()

            self.stdout.write(self.style.ERROR(f"\n  Erreur sync ENOC Fuel : {exc}\n"))
            raise