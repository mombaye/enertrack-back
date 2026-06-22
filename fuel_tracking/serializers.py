from rest_framework import serializers
from .models import FuelEfmsMonthly, FuelEfmsSyncRun


class FuelEfmsMonthlySerializer(serializers.ModelSerializer):
    class Meta:
        model = FuelEfmsMonthly
        fields = [
            "id",
            "month_year",
            "year",
            "month",
            "country",
            "site_id",
            "site_name",
            "fuel_order_l",
            "fuel_deli_l",
            "fuel_conso_l",
            "ge_working_hours",
            "abnormal_ge_working_hours",
            "monitoring_unavailability_hours",
            "monitoring_unavailability_percent",
            "cph_l_per_hour",
            "anomaly_flags",
            "synced_at",
            "updated_at",
        ]


# fuel_tracking/serializers.py

from rest_framework import serializers

from fuel_tracking.models import (
    FuelEnocMovement,
    FuelEnocSyncRun,
    FuelEfmsSyncRun,
)


class FuelEnocMovementSerializer(serializers.ModelSerializer):
    class Meta:
        model = FuelEnocMovement
        fields = [
            "id",
            "source_system",
            "source_id",
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
            "comment",
            "synced_at",
            "updated_at",
        ]


class FuelEnocSyncRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = FuelEnocSyncRun
        fields = [
            "id",
            "start_date",
            "end_date",
            "updated_since",
            "status",
            "rows_fetched",
            "rows_created",
            "rows_updated",
            "started_at",
            "finished_at",
            "error_message",
        ]


class FuelEfmsSyncRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = FuelEfmsSyncRun
        fields = [
            "id",
            "country",
            "start_month",
            "end_month",
            "status",
            "rows_fetched",
            "rows_created",
            "rows_updated",
            "started_at",
            "finished_at",
            "error_message",
        ]

