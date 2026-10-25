from sqlalchemy import select
from management.models import PrometheusAlertState
from management.domain import utc

RULES={
    'MonitoringTargetDown':'monitoring_target_down',
    'PortalTelemetryMissing':'portal_telemetry_missing',
    'PortalHighServerErrorRate':'portal_high_error_rate',
}
RULE_IDS=set(RULES.values())

def recovery_ready(db,incident,at):
    state=db.scalar(select(PrometheusAlertState).where(PrometheusAlertState.target_key==incident.target_id))
    return bool(state and state.incident_id==incident.id and state.state=='normal' and state.normal_samples>=2 and state.evaluated_at and -5<=(at-utc(state.evaluated_at)).total_seconds()<=60)
