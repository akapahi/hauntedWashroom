// Minimal ESP32 example: call triggerEntity() from your sensor code.
// The server ignores triggers while a conversation or cooldown is running,
// so it's safe to call this on every detection.

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>

const char* WIFI_SSID     = "your-wifi";
const char* WIFI_PASSWORD = "your-password";
// Hosted on Render:  "https://<your-service>.onrender.com/api/trigger?sensor=washroom-door"
// On the LAN:        "http://192.168.1.50:8000/api/trigger?sensor=washroom-door" (the IP is in the dashboard logs on startup)
const char* ENTITY_URL    = "https://your-service.onrender.com/api/trigger?sensor=washroom-door";
const char* TRIGGER_TOKEN = "";  // same value as TRIGGER_TOKEN on the server (leave "" if unset)

void connectWifi() {
  if (WiFi.status() == WL_CONNECTED) return;
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) delay(250);
}

// Returns true if the server started a conversation.
bool triggerEntity() {
  connectWifi();
  if (WiFi.status() != WL_CONNECTED) return false;

  HTTPClient http;
  WiFiClientSecure secureClient;
  WiFiClient plainClient;
  bool https = strncmp(ENTITY_URL, "https://", 8) == 0;
  if (https) secureClient.setInsecure();  // skip certificate checks; use setCACert() with Render's root CA to be strict
  if (!(https ? http.begin(secureClient, ENTITY_URL) : http.begin(plainClient, ENTITY_URL))) return false;
  http.setTimeout(20000);  // a sleeping free Render instance can take a while to answer the first request
  if (strlen(TRIGGER_TOKEN) > 0) http.addHeader("X-Trigger-Token", TRIGGER_TOKEN);
  int code = http.POST("");
  String body = http.getString();  // {"accepted":true|false,"reason":"...","state":"..."}
  http.end();

  Serial.printf("trigger -> HTTP %d %s\n", code, body.c_str());
  return code == 200 && body.indexOf("\"accepted\":true") >= 0;
}

// ---- example only: replace with your sensor logic ----
const int SENSOR_PIN = 13;   // e.g. PIR output
int lastLevel = LOW;

void setup() {
  Serial.begin(115200);
  pinMode(SENSOR_PIN, INPUT);
  connectWifi();
}

void loop() {
  int level = digitalRead(SENSOR_PIN);
  if (level == HIGH && lastLevel == LOW) triggerEntity();  // rising edge
  lastLevel = level;
  delay(50);
}
