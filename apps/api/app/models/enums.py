import enum


class UserRole(str, enum.Enum):
    OWNER = "owner"
    ADMIN = "admin"
    MANAGER = "manager"
    AGENT_VIEWER = "agent_viewer"


class CRMProviderType(str, enum.Enum):
    HUBSPOT = "hubspot"
    SALESFORCE = "salesforce"
    ZOHO = "zoho"
    PIPEDRIVE = "pipedrive"
    GOOGLE_SHEETS = "google_sheets"
    CSV = "csv"
    CUSTOM_API = "custom_api"
    MOCK = "mock"


class CRMConnectionStatus(str, enum.Enum):
    PENDING = "pending"          # OAuth/config not completed yet
    CONNECTED = "connected"      # credentials valid, no sync has run yet
    IMPORTING = "importing"      # initial sync in progress
    SYNCED = "synced"            # at least one sync (initial or incremental) has succeeded
    ERROR = "error"              # last sync failed / credentials invalid
    REAUTH_REQUIRED = "reauthorization_required"  # OAuth token expired or revoked
    DISCONNECTED = "disconnected"


class SyncJobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class CRMEventStatus(str, enum.Enum):
    RECEIVED = "received"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"
    DUPLICATE = "duplicate"


class CallingStatus(str, enum.Enum):
    NOT_CALLED = "not_called"
    QUEUED = "queued"
    CALLING = "calling"
    CALLED = "called"
    CALLBACK_SCHEDULED = "callback_scheduled"
    DO_NOT_CALL = "do_not_call"
    EXHAUSTED = "exhausted"


class CallDirection(str, enum.Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class CallStatus(str, enum.Enum):
    QUEUED = "queued"
    DIALING = "dialing"
    RINGING = "ringing"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    FAILED = "failed"
    CANCELED = "canceled"


class CallJobStatus(str, enum.Enum):
    PENDING = "pending"
    LOCKED = "locked"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    CANCELED = "canceled"
    FAILED = "failed"


class KnowledgeSourceType(str, enum.Enum):
    DOCUMENT = "document"
    WEBSITE = "website"
    FAQ = "faq"
    PRODUCT = "product"
    SERVICE = "service"
    CUSTOM_TEXT = "custom_text"


class KnowledgeStatus(str, enum.Enum):
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"


class MeetingStatus(str, enum.Enum):
    REQUESTED = "requested"
    BOOKED = "booked"
    COMPLETED = "completed"
    CANCELED = "canceled"
    RESCHEDULED = "rescheduled"


class WhatsAppMessageStatus(str, enum.Enum):
    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"


class CallbackStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    DUE = "due"
    COMPLETED = "completed"
    CANCELED = "canceled"
