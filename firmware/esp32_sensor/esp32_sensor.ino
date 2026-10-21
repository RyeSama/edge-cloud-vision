/*
 * ESP32 sensor node for the Edge-Cloud Vision project.
 *
 * It does not run a camera or a model. It samples light + distance, timestamps
 * with a monotonic microsecond counter, and prints one compact JSON object per
 * line at 115200 8N1. The PC validates it with scripts/serial_monitor.py.
 *
 * Libraries (Arduino IDE -> Library Manager):
 *   - BH1750 by Christopher Laws
 *
 * Board: ESP32 Dev Module. See README.md for wiring and upload steps.
 */

#include <Arduino.h>
#include <Wire.h>
#include <BH1750.h>
#include <esp_random.h>
#include <esp_timer.h>

static const char *DEVICE_ID = "esp32-01";
static const uint8_t I2C_SDA = 21;
static const uint8_t I2C_SCL = 22;
static const uint8_t TRIG_PIN = 5;
static const uint8_t ECHO_PIN = 18;
static const uint8_t BUTTON_PIN = 4;
static const uint8_t LED_PIN = 2;

BH1750 lightMeter(0x23);
char bootId[9];
uint32_t seq = 0;

static float readDistanceMm() {
  digitalWrite(TRIG_PIN, LOW);
  delayMicroseconds(2);
  digitalWrite(TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);

  const unsigned long duration = pulseIn(ECHO_PIN, HIGH, 30000UL);
  if (duration == 0) {
    return NAN;  // no echo: out of range or wiring problem
  }
  return duration * 0.343f / 2.0f;  // speed of sound ~343 m/s at 20 C
}

static void formatFloat(char *out, size_t size, float value, uint8_t digits) {
  if (isnan(value)) {
    snprintf(out, size, "null");
    return;
  }
  snprintf(out, size, "%.*f", digits, value);
}

void setup() {
  Serial.begin(115200);
  delay(300);

  pinMode(TRIG_PIN, OUTPUT);
  pinMode(ECHO_PIN, INPUT);
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  Wire.begin(I2C_SDA, I2C_SCL);
  if (!lightMeter.begin(BH1750::CONTINUOUS_HIGH_RES_MODE)) {
    Serial.println("# BH1750 not found; check SDA/SCL and address 0x23");
  }

  // boot_id changes on every reset, so the host can tell a reboot from a gap.
  const uint32_t token = esp_random() ^ (uint32_t)esp_timer_get_time();
  snprintf(bootId, sizeof(bootId), "%08lx", (unsigned long)token);
  Serial.printf("# boot_id=%s device_id=%s\n", bootId, DEVICE_ID);
}

void loop() {
  const float lux = lightMeter.readLightLevel();
  const float distanceMm = readDistanceMm();
  const bool trigger = digitalRead(BUTTON_PIN) == LOW;
  const int64_t tUs = esp_timer_get_time();

  char luxText[16];
  char distanceText[16];
  formatFloat(luxText, sizeof(luxText), lux, 1);
  formatFloat(distanceText, sizeof(distanceText), distanceMm, 1);

  digitalWrite(LED_PIN, trigger ? HIGH : LOW);
  Serial.printf(
      "{\"schema_version\":\"1.0\",\"kind\":\"sensor\",\"device_id\":\"%s\","
      "\"boot_id\":\"%s\",\"seq\":%lu,\"t_us\":%lld,\"lux\":%s,"
      "\"distance_mm\":%s,\"trigger\":%s}\n",
      DEVICE_ID, bootId, (unsigned long)seq++, (long long)tUs, luxText,
      distanceText, trigger ? "true" : "false");

  delay(100);  // 10 Hz; change to 50 for 20 Hz if your sensor allows
}
