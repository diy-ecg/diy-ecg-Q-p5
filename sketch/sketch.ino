// Arduino UNO Q (STM32U585, Zephyr-based core) ECG sampler with Bridge/RPC frame fetch.
//
// Collects analog samples from A0 at 200 Hz into a ring buffer and exposes
// an RPC method `ecg_get_frame` that returns the pending frame as MsgPack binary.
// Frame layout (little-endian):
// [uint8 count][uint32 t0_ms][count * (uint16 sample + uint8 dt_ms)]
// dt_ms is the delta to the previous sample timestamp, saturated at 255.
//
// On the Linux side, call:
//   frame = Bridge.call("ecg_get_frame")
//   # frame is a MsgPack bin payload; parse bytes directly.
//
// The timer callback only schedules samples (Zephyr k_timer); the actual ADC
// read happens in loop() to keep the callback itself short.

#include <Arduino.h>
#include <Arduino_RouterBridge.h>
#include <MsgPack.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/atomic.h>

const uint16_t SAMPLE_INTERVAL_US = 5000;  // 5 ms -> 200 Hz
const uint16_t RING_SIZE          = 200;   // max samples kept between requests (~1 s)

struct Sample {
  uint16_t value;
  uint32_t t_ms;
};

volatile Sample  ringBuf[RING_SIZE];
volatile uint16_t head       = 0;  // next write position
volatile uint16_t last_sent  = 0;  // position after last sent sample
volatile bool     overflowed = false;

// Timing / scheduling
static struct k_timer sampleTimer;
atomic_t timer_ticks = ATOMIC_INIT(0);
uint32_t sample_time_us = 0;

static void onSampleTimer(struct k_timer *timer_id) {
  (void)timer_id;
  atomic_inc(&timer_ticks);
}

void readADC(uint32_t t_ms);
MsgPack::bin_t<uint8_t> ecg_get_frame();

void setup() {
  Bridge.begin();
  Bridge.provide("ecg_get_frame", ecg_get_frame);

  // Periodic 200 Hz tick using Zephyr kernel timer
  k_timer_init(&sampleTimer, onSampleTimer, nullptr);
  k_timer_start(&sampleTimer,
                K_USEC(SAMPLE_INTERVAL_US),   // init time
                K_USEC(SAMPLE_INTERVAL_US));  // sample period
  sample_time_us = 0;
}

void loop() {
  uint32_t pending_samples = atomic_set(&timer_ticks, 0);
  if (pending_samples == 0) {
    k_sleep(K_USEC(500));
  }
  else {
    while (pending_samples > 0) {
      sample_time_us += SAMPLE_INTERVAL_US;
      readADC(sample_time_us / 1000);  // store ms timestamps
      pending_samples--;
    }
  }
  // Bridge background thread handles RPC requests.
}

void readADC(uint32_t t_ms) {
  uint16_t next = head + 1;
  if (next == RING_SIZE) next = 0;

  // If the ring would overwrite unsent data, drop the oldest (move last_sent forward)
  if (next == last_sent) {
    uint16_t new_last_sent = last_sent + 1;
    if (new_last_sent == RING_SIZE) new_last_sent = 0;
    last_sent = new_last_sent;
    overflowed = true;
  }

  ringBuf[head].value = analogRead(A0);
  ringBuf[head].t_ms  = t_ms;
  head = next;
}

// Basic RPC method to get pending ECG frame as MsgPack binary.
MsgPack::bin_t<uint8_t> ecg_get_frame() {
  // Snapshot indices atomically
  noInterrupts();
  uint16_t tail = last_sent;
  uint16_t h    = head;
  bool ovf      = overflowed;
  overflowed    = false;
  interrupts();

  MsgPack::bin_t<uint8_t> out;
  uint16_t count = (h >= tail) ? (h - tail) : (RING_SIZE - tail + h);
  if (count == 0) {
    return out;  // empty payload, no data
  }
  if (count > 255) count = 255;  // clamp to fit in uint8 count

  // Size per sample: 2 (value) + 1 (dt_ms) = 3 bytes
  // Buffer big enough for worst-case payload: 1 + 4 + 3*255 = 770 bytes
  static uint8_t frame[800];
  size_t idx = 0;

  frame[idx++] = static_cast<uint8_t>(count);

  // Frame start timestamp (t0)
  uint32_t t0 = ringBuf[tail].t_ms;
  frame[idx++] = t0 & 0xFF;
  frame[idx++] = (t0 >> 8) & 0xFF;
  frame[idx++] = (t0 >> 16) & 0xFF;
  frame[idx++] = (t0 >> 24) & 0xFF;

  // Send samples and delta timestamps in order from tail
  uint16_t i_idx = tail;
  uint32_t prev_t = t0;
  for (uint16_t i = 0; i < count; i++) {
    if (i_idx == RING_SIZE) i_idx = 0;

    uint16_t v = ringBuf[i_idx].value;
    uint32_t t = ringBuf[i_idx].t_ms;

    frame[idx++] = v & 0xFF;
    frame[idx++] = (v >> 8) & 0xFF;

    uint32_t dt = t - prev_t;
    frame[idx++] = (i == 0) ? 0 : (dt > 255 ? 255 : static_cast<uint8_t>(dt));
    prev_t = t;

    i_idx++;
  }

  // Advance last_sent to h (or tail + count if clamped)
  noInterrupts();
  last_sent = (tail + count) % RING_SIZE;
  interrupts();

  // If overflow happened, optionally prepend "!" marker (0x21) to signal host.
  if (ovf) {
    out.push_back(0x21);
  }
  for (size_t i = 0; i < idx; i++) {
    out.push_back(frame[i]);
  }
  return out;
}
