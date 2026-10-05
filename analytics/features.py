"""
Event -> feature mapping shared by the Python analytics.
Must match dbt_project/models/intermediate/int_feature_usage.sql.
"""
FEATURE_MAP = {
    'ai_assist.used': 'ai_assist',
    'ai_assist.summary_generated': 'ai_assist',
    'ai_voice_agent.activated': 'ai_voice_agent',
    'ai_voice_agent.call_handled': 'ai_voice_agent',
    'call.started': 'voice_calls',
    'call.ended': 'voice_calls',
    'call.recorded': 'call_recording',
    'sms.sent': 'sms',
    'sms.received': 'sms',
    'whatsapp.sent': 'whatsapp',
    'whatsapp.received': 'whatsapp',
    'integration.installed': 'integrations',
    'integration.configured': 'integrations',
}
AI_FEATURES = ('ai_assist', 'ai_voice_agent')
