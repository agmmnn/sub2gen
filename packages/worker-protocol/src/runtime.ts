import { MESSAGE_TYPES, PROTOCOL_VERSION, type Envelope, type MessageType } from "./generated"

const MESSAGE_TYPE_SET = new Set<string>(MESSAGE_TYPES)

export class ProtocolCodecError extends Error {}

export function decodeEnvelope(frame: string): Envelope {
  let value: unknown
  try {
    value = JSON.parse(frame)
  } catch {
    throw new ProtocolCodecError("invalid worker protocol JSON frame")
  }
  return validateEnvelope(value)
}

export function validateEnvelope(value: unknown): Envelope {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new ProtocolCodecError("worker protocol frame must be an object")
  }
  const record = value as Record<string, unknown>
  const required = [
    "protocol_version",
    "message_id",
    "message_type",
    "correlation_id",
    "job_id",
    "job_kind",
    "worker_id",
    "sent_at",
    "payload",
  ] as const
  if (required.some((key) => !(key in record))) {
    throw new ProtocolCodecError("worker protocol envelope is incomplete")
  }
  if (record.protocol_version !== PROTOCOL_VERSION) {
    throw new ProtocolCodecError("unsupported worker protocol version")
  }
  if (!MESSAGE_TYPE_SET.has(String(record.message_type))) {
    throw new ProtocolCodecError("unsupported worker protocol message type")
  }
  if (typeof record.message_id !== "string" || !record.message_id.trim()) {
    throw new ProtocolCodecError("message_id must not be empty")
  }
  if (typeof record.worker_id !== "string" || !record.worker_id.trim()) {
    throw new ProtocolCodecError("worker_id must not be empty")
  }
  if (!record.payload || typeof record.payload !== "object" || Array.isArray(record.payload)) {
    throw new ProtocolCodecError("payload must be an object")
  }
  const messageType = record.message_type as MessageType
  if (
    messageType.startsWith("job.") &&
    (typeof record.job_id !== "string" || !record.job_id || typeof record.job_kind !== "string" || !record.job_kind)
  ) {
    throw new ProtocolCodecError("job messages require job_id and job_kind")
  }
  return record as unknown as Envelope
}

export function encodeEnvelope(envelope: Envelope): string {
  return JSON.stringify(validateEnvelope(envelope))
}

export function makeEnvelope<TPayload extends Record<string, unknown>>(
  messageType: MessageType,
  workerId: string,
  payload: TPayload,
  options: {
    correlationId?: string | null
    jobId?: string | null
    jobKind?: string | null
    messageId?: string
    sentAt?: string
  } = {},
): Envelope<TPayload> {
  return validateEnvelope({
    protocol_version: PROTOCOL_VERSION,
    message_id: options.messageId ?? `msg_${crypto.randomUUID().replaceAll("-", "")}`,
    message_type: messageType,
    correlation_id: options.correlationId ?? null,
    job_id: options.jobId ?? null,
    job_kind: options.jobKind ?? null,
    worker_id: workerId,
    sent_at: options.sentAt ?? new Date().toISOString(),
    payload,
  }) as Envelope<TPayload>
}

export function negotiateProtocol(supportedVersions: readonly string[] | undefined): "1.0" {
  if (supportedVersions === undefined) throw new ProtocolCodecError("worker protocol version is required")
  if (supportedVersions.includes(PROTOCOL_VERSION)) return PROTOCOL_VERSION
  throw new ProtocolCodecError("worker does not support a server protocol version")
}
