// Fault-injection helpers for the direct provider wrapper (wrap.ts). Reuses the
// parity support module's seam/stream doubles and adds a CaptureRuntime facade
// whose start()/finish() can be made to throw, plus a strict expected-failure
// helper that pins a currently-failing behavioral contract.

import assert from "node:assert/strict";

import { setCaptureRuntime } from "../../dist/wrap.js";
import { recordingRuntime } from "./instrumentation-doubles.mjs";

// A CaptureRuntime facade over a recording runtime whose start()/finish() throw
// the supplied errors. With neither set it is a healthy runtime, so one fixture
// drives both fault and control cases.
export function faultRuntime({ startError, finishError } = {}) {
  const { runtime, rows } = recordingRuntime();
  const facade = {
    start(...args) {
      if (startError) throw startError;
      return runtime.start(...args);
    },
    finish(...args) {
      if (finishError) throw finishError;
      return runtime.finish(...args);
    },
  };
  return { runtime: facade, rows };
}

// Installs a fault runtime as the active capture runtime for a test and returns
// the recorded rows.
export function installFaultRuntime(t, options = {}) {
  const { runtime, rows } = faultRuntime(options);
  setCaptureRuntime(runtime);
  t.after(() => setCaptureRuntime());
  return rows;
}

// A streaming chunk whose text-bearing field throws on access. Capture reads
// this field during classification and tool extraction; a consumer need not.
export function fieldFaultChunk(error) {
  return {
    type: "content_block_delta",
    get delta() { throw error; },
  };
}

// Runs a contract that asserts the desired fail-open behavior. Current wrap.ts
// violates it, so the contract must trip a node:assert AssertionError whose
// message contains `failsWith`. A different assertion or a non-assertion error
// is surfaced; a passing contract fails the test so the marker is removed once
// the production fix lands.
export async function expectContractViolation(contract, { failsWith } = {}) {
  try {
    await contract();
  } catch (error) {
    if (!(error instanceof assert.AssertionError)) throw error;
    if (failsWith !== undefined && !error.message.includes(failsWith)) {
      throw new assert.AssertionError({
        message:
          "expected-failure contract tripped a different assertion than the known gap\n"
          + `  expected substring: ${JSON.stringify(failsWith)}\n`
          + `  actual: ${error.message}`,
      });
    }
    return;
  }
  assert.fail(
    "expected-failure contract now holds; remove this marker and keep the contract as a plain test",
  );
}
