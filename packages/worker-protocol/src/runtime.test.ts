import { describe, expect, test } from "vitest"
import transcripts from "../schema/golden-transcripts.json"
import { decodeEnvelope, encodeEnvelope, negotiateProtocol, ProtocolCodecError } from "./runtime"

describe("worker protocol v1", () => {
  test("round-trips the shared golden transcripts", () => {
    for (const value of Object.values(transcripts)) {
      const decoded = decodeEnvelope(JSON.stringify(value))
      expect(JSON.parse(encodeEnvelope(decoded))).toEqual(value)
    }
  })

  test("requires an explicit supported v1 version", () => {
    expect(negotiateProtocol(["1.0"])).toBe("1.0")
    expect(() => negotiateProtocol(undefined)).toThrow(ProtocolCodecError)
    expect(() => negotiateProtocol(["2.0"])).toThrow(ProtocolCodecError)
  })

  test("rejects unknown versions and incomplete job identity", () => {
    expect(() => decodeEnvelope(JSON.stringify({ ...transcripts.hello, protocol_version: "2.0" }))).toThrow()
    expect(() => decodeEnvelope(JSON.stringify({ ...transcripts.offer, job_id: null }))).toThrow()
  })
})
