import {
  PROTOCOL_VERSION,
  decodeEnvelope,
  encodeEnvelope,
  makeEnvelope,
  type Envelope,
  type JobCancelPayload,
  type JobDecisionPayload,
  type JobErrorPayload,
  type JobOfferPayload,
  type JobResultPayload,
  type WorkerChallengePayload,
  type WorkerHeartbeatPayload,
  type WorkerHelloPayload,
  type WorkerRegisterPayload,
  type WorkerRegisteredPayload,
} from "@sub2gen/worker-protocol"

export const CAPTCHA_CAPABILITY = "captcha.solve:google-flow"
export const REFRESH_CAPABILITY = "session.refresh:google-flow"
export const RELAY_CAPABILITY = "http.relay:google-flow"

export interface WorkerIdentity {
  workerId: string
  publicKey: string
  privateKey: string
}

export interface PairWorkerInput {
  httpBaseUrl: string
  pairingCode: string
  workerId: string
  label: string
  capabilities: readonly string[]
}

function bytesToBase64(bytes: Uint8Array): string {
  let value = ""
  for (const byte of bytes) value += String.fromCharCode(byte)
  return btoa(value)
}

function base64ToBytes(value: string): Uint8Array<ArrayBuffer> {
  const decoded = atob(value)
  const bytes = new Uint8Array(decoded.length)
  for (let index = 0; index < decoded.length; index += 1) bytes[index] = decoded.charCodeAt(index)
  return bytes
}

async function generateIdentity(workerId: string): Promise<WorkerIdentity> {
  const pair = await crypto.subtle.generateKey({ name: "Ed25519" }, true, ["sign", "verify"])
  const publicKey = await crypto.subtle.exportKey("raw", pair.publicKey)
  const privateKey = await crypto.subtle.exportKey("pkcs8", pair.privateKey)
  return {
    workerId,
    publicKey: bytesToBase64(new Uint8Array(publicKey)),
    privateKey: bytesToBase64(new Uint8Array(privateKey)),
  }
}

export async function pairWorker(fetcher: typeof fetch, input: PairWorkerInput): Promise<WorkerIdentity> {
  const identity = await generateIdentity(input.workerId)
  const response = await fetcher(`${input.httpBaseUrl.replace(/\/$/, "")}/api/workers/pair`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      pairing_code: input.pairingCode,
      worker_id: identity.workerId,
      kind: "chrome-extension",
      label: input.label || "Chrome worker",
      public_key: identity.publicKey,
      capabilities: input.capabilities,
    }),
  })
  if (!response.ok) {
    const payload = await response.json().catch(() => ({})) as { detail?: string }
    throw new Error(payload.detail || `Pairing failed (HTTP ${response.status})`)
  }
  return identity
}

export async function signChallenge(identity: WorkerIdentity, challenge: WorkerChallengePayload): Promise<string> {
  const privateKey = await crypto.subtle.importKey(
    "pkcs8",
    base64ToBytes(identity.privateKey),
    { name: "Ed25519" },
    false,
    ["sign"],
  )
  const nonce = base64ToBytes(challenge.nonce)
  const prefix = new TextEncoder().encode(`${challenge.challenge_id}.`)
  const signed = new Uint8Array(prefix.length + nonce.length)
  signed.set(prefix)
  signed.set(nonce, prefix.length)
  return bytesToBase64(new Uint8Array(await crypto.subtle.sign("Ed25519", privateKey, signed)))
}

export interface CanonicalWorkerHandlers {
  execute(offer: JobOfferPayload, envelope: Envelope): Promise<Record<string, unknown>>
  cancel?(offer: JobCancelPayload, envelope: Envelope): Promise<void>
}

export class CanonicalWorkerConnection {
  private socket: WebSocket | null = null
  private heartbeat: ReturnType<typeof setInterval> | null = null
  private readonly activeLeases = new Set<string>()
  private registered = false

  constructor(
    private readonly url: string,
    private readonly identity: WorkerIdentity,
    private readonly instanceId: string,
    private readonly capabilities: readonly string[],
    private readonly handlers: CanonicalWorkerHandlers,
    private readonly onState: (state: string, detail?: string) => void,
  ) {}

  connect(): void {
    this.close()
    this.onState("connecting")
    const socket = new WebSocket(this.url)
    this.socket = socket
    socket.onopen = () => {
      const payload: WorkerHelloPayload = {
        supported_versions: [PROTOCOL_VERSION], worker_kind: "chrome-extension", instance_id: this.instanceId,
      }
      socket.send(encodeEnvelope(makeEnvelope("worker.hello", this.identity.workerId, payload)))
      this.onState("open")
    }
    socket.onmessage = (event) => void this.handle(String(event.data))
    socket.onerror = () => this.onState("error", "websocket_error")
    socket.onclose = () => {
      this.registered = false
      if (this.heartbeat) clearInterval(this.heartbeat)
      this.heartbeat = null
      this.onState("closed")
    }
  }

  close(): void {
    if (this.heartbeat) clearInterval(this.heartbeat)
    this.heartbeat = null
    this.socket?.close()
    this.socket = null
    this.registered = false
  }

  private send(envelope: Envelope): void {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) throw new Error("worker socket is closed")
    this.socket.send(encodeEnvelope(envelope))
  }

  private async handle(frame: string): Promise<void> {
    const envelope = decodeEnvelope(frame)
    if (envelope.worker_id !== this.identity.workerId) throw new Error("worker identity mismatch")
    if (envelope.message_type === "worker.challenge") {
      const challenge = envelope.payload as WorkerChallengePayload
      const payload: WorkerRegisterPayload = {
        selected_version: PROTOCOL_VERSION,
        challenge_id: challenge.challenge_id,
        signature: await signChallenge(this.identity, challenge),
        capabilities: this.capabilities,
        worker_session_id: crypto.randomUUID(),
      }
      this.send(makeEnvelope("worker.register", this.identity.workerId, payload, { correlationId: envelope.message_id }))
      return
    }
    if (envelope.message_type === "worker.registered") {
      const payload = envelope.payload as WorkerRegisteredPayload
      if (payload.selected_version !== PROTOCOL_VERSION) throw new Error("server selected an unsupported protocol")
      this.registered = true
      this.onState("registered")
      this.heartbeat = setInterval(() => this.sendHeartbeat(payload.worker_session_id), 10_000)
      this.sendHeartbeat(payload.worker_session_id)
      return
    }
    if (envelope.message_type === "job.offer") {
      await this.executeOffer(envelope, envelope.payload as JobOfferPayload)
      return
    }
    if (envelope.message_type === "job.cancel") {
      await this.handlers.cancel?.(envelope.payload as JobCancelPayload, envelope)
    }
  }

  private sendHeartbeat(workerSessionId: string): void {
    if (!this.registered) return
    const payload: WorkerHeartbeatPayload = {
      worker_session_id: workerSessionId,
      active_leases: [...this.activeLeases],
      available_slots: this.activeLeases.size === 0 ? 1 : 0,
    }
    this.send(makeEnvelope("worker.heartbeat", this.identity.workerId, payload))
  }

  private async executeOffer(envelope: Envelope, offer: JobOfferPayload): Promise<void> {
    const options = { correlationId: envelope.message_id, jobId: envelope.job_id, jobKind: envelope.job_kind }
    if (!this.capabilities.includes(offer.capability)) {
      const rejected: JobDecisionPayload = { attempt: offer.attempt, lease_id: offer.lease_id, reason: "capability_not_enabled" }
      this.send(makeEnvelope("job.reject", this.identity.workerId, rejected, options))
      return
    }
    const accepted: JobDecisionPayload = { attempt: offer.attempt, lease_id: offer.lease_id }
    this.send(makeEnvelope("job.accept", this.identity.workerId, accepted, options))
    this.activeLeases.add(offer.lease_id)
    try {
      const output = await this.handlers.execute(offer, envelope)
      const result: JobResultPayload = { attempt: offer.attempt, lease_id: offer.lease_id, output }
      this.send(makeEnvelope("job.result", this.identity.workerId, result, options))
    } catch (error) {
      const payload: JobErrorPayload = {
        attempt: offer.attempt,
        lease_id: offer.lease_id,
        error: {
          code: "worker_execution_failed",
          message: error instanceof Error ? error.message : String(error),
          retryable: false,
          detail: {},
        },
      }
      this.send(makeEnvelope("job.error", this.identity.workerId, payload, options))
    } finally {
      this.activeLeases.delete(offer.lease_id)
    }
  }
}
