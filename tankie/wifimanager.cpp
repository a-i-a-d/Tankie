#include "wifimanager.h"
#include "netstate.h"
#include "wificfg.h"

// How long to wait for the stored network before falling back to the portal.
static const unsigned long STA_CONNECT_TIMEOUT_MS = 15000;
// How long after saving the config to wait before rebooting (lets the
// HTTP response "Done. ESP will restart..." reach the browser first).
static const unsigned long REBOOT_DELAY_MS = 3000;

WiFiManager::WiFiManager() {}

// ---------------------------------------------------------------------------
// Config load/save (EEPROM sector 0x3FB000 - the single source of truth)
// ---------------------------------------------------------------------------

// Load the stored configuration from the EEPROM sector (issue #54). A blank
// or corrupt sector (magic + CRC check fails) means "no config" -> return
// false so the config portal is started.
bool WiFiManager::loadStoredConfig() {
  WifiCfg cfg;
  if (wifiCfgLoad(&cfg)) {
    _ssid = cfg.ssid;
    _pass = cfg.pass;
    _ip = cfg.ip;
    _gateway = cfg.gateway;
    Serial.println("[WiFiManager] loaded config from EEPROM sector 0x3FB000");
    return true;
  }
  Serial.println("[WiFiManager] no stored config (blank/corrupt EEPROM sector) - starting config portal");
  return false;
}

// Persist the stored configuration to the EEPROM sector (single source of
// truth, issue #54). Returns true on success.
bool WiFiManager::saveStoredConfig() {
  WifiCfg cfg;
  cfg.ssid = _ssid;
  cfg.pass = _pass;
  cfg.ip = _ip;
  cfg.gateway = _gateway;
  bool ok = wifiCfgSave(cfg);
  Serial.printf("[WiFiManager] %s config to EEPROM sector 0x3FB000\n",
                ok ? "saved" : "FAILED to save");
  return ok;
}

// Clear the stored configuration (factory reset): wipe the EEPROM sector
// and the in-memory copy, so the next boot opens the config portal.
void WiFiManager::clearStoredConfig() {
  wifiCfgWipe();
  _ssid = String();
  _pass = String();
  _ip = String();
  _gateway = String();
  Serial.println("[WiFiManager] stored config cleared (EEPROM sector wiped)");
}

// ---------------------------------------------------------------------------
// STA connection
// ---------------------------------------------------------------------------

bool WiFiManager::connectSTA(unsigned long timeoutMs) {
  if (_ssid.length() == 0) {
    Serial.println("[WiFiManager] no SSID stored - starting config portal");
    return false;
  }

  Serial.printf("[WiFiManager] connecting to \"%s\" ...", _ssid.c_str());

  WiFi.mode(WIFI_STA);

  // Optional static IP (only applied when an IP was actually configured;
  // leave the "ip" field empty or set it to "dhcp" for DHCP).
  IPAddress localIP;
  IPAddress localGateway;
  IPAddress subnet(255, 255, 255, 0);
  if (_ip.length() > 0 && _ip != "dhcp" && localIP.fromString(_ip.c_str())) {
    localGateway.fromString(_gateway.c_str());
    if (localGateway == IPAddress(0, 0, 0, 0)) {
      // No gateway given: derive it from the IP (x.x.x.1).
      localGateway = localIP;
      localGateway[3] = 1;
    }
    if (!WiFi.config(localIP, localGateway, subnet)) {
      Serial.println("[WiFiManager] warning: WiFi.config() failed, continuing with DHCP");
    }
  } else if (_ip.length() > 0 && _ip != "dhcp") {
    Serial.printf("[WiFiManager] invalid stored IP \"%s\", using DHCP\n", _ip.c_str());
  }

  WiFi.begin(_ssid.c_str(), _pass.c_str());

  // Wait for the connection without blocking the whole system forever.
  const unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED) {
    if (millis() - start > timeoutMs) {
      Serial.println();
      Serial.println("[WiFiManager] STA connection timed out");
      return false;
    }
    delay(250);
    Serial.print(".");
  }
  Serial.println();
  Serial.println("[WiFiManager] WiFi connected");
  IPAddress staIp = WiFi.localIP();
  Serial.print("[WiFiManager] IP address: ");
  Serial.println(staIp);

  // Issue #46: publish the network state so clients can read it from the
  // state broadcast (serial + websocket) without needing the boot console.
  netState.mode  = "sta";
  netState.ip    = staIp.toString();
  netState.ssid  = _ssid;

  return true;
}

// ---------------------------------------------------------------------------
// Config portal (fallback AP)
// ---------------------------------------------------------------------------

void WiFiManager::startPortal() {
  Serial.println("[WiFiManager] STA failed - starting config portal AP");
  Serial.printf("[WiFiManager] AP SSID: \"%s\"", _apSsid.c_str());
  if (_apPassword.length() > 0) {
    Serial.printf(", password: \"%s\"", _apPassword.c_str());
  }
  Serial.println();

  IPAddress apIP(192, 168, 4, 1);
  IPAddress apGateway(192, 168, 4, 1);
  IPAddress apSubnet(255, 255, 255, 0);

  WiFi.mode(WIFI_AP);
  WiFi.softAPConfig(apIP, apGateway, apSubnet);
  if (_apPassword.length() > 0) {
    WiFi.softAP(_apSsid.c_str(), _apPassword.c_str());
  } else {
    WiFi.softAP(_apSsid.c_str());
  }

  IPAddress myIP = WiFi.softAPIP();
  Serial.print("[WiFiManager] AP IP address: ");
  Serial.println(myIP);
  Serial.println("[WiFiManager] open the browser at http://192.168.4.1:8080 to configure WiFi");

  // Issue #46: publish the AP state so clients can read it from the state
  // broadcast (serial + websocket) without needing the boot console.
  netState.mode  = "ap";
  netState.ip    = myIP.toString();
  netState.ssid  = "";

  _inConfigMode = true;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

bool WiFiManager::begin(const char* apSsid, const char* apPassword) {
  _apSsid = (apSsid != NULL) ? String(apSsid) : String("tankie-esp");
  _apPassword = (apPassword != NULL) ? String(apPassword) : String();

  // Load the stored configuration (EEPROM sector 0x3FB000 - issue #54).
  loadStoredConfig();
  Serial.printf("[WiFiManager] stored ssid=%s ip=%s gateway=%s\n",
                _ssid.c_str(), _ip.c_str(), _gateway.c_str());

  if (connectSTA(STA_CONNECT_TIMEOUT_MS)) {
    return true;
  }
  startPortal();
  return false;
}

void WiFiManager::loop() {
  if (_rebootPending) {
    Serial.println("[WiFiManager] config saved - rebooting to apply");
    delay(REBOOT_DELAY_MS);
    ESP.restart();
  }
}

void WiFiManager::handleConfigPost(AsyncWebServerRequest* request) {
  for (size_t i = 0; i < request->params(); i++) {
    const AsyncWebParameter* p = request->getParam(i);
    if (!p->isPost()) {
      continue;
    }
    String name = p->name();
    String value = p->value();
    Serial.printf("[WiFiManager] POST %s = %s\n", name.c_str(), value.c_str());

    if (name == "ssid") {
      _ssid = value;
    } else if (name == "pass") {
      _pass = value;
    } else if (name == "ip") {
      _ip = value;
    } else if (name == "gateway") {
      _gateway = value;
    }
  }

  if (request->hasParam("reset", true)) {
    // "Factory reset": wipe the stored network configuration (EEPROM sector).
    Serial.println("[WiFiManager] reset requested - clearing stored config");
    clearStoredConfig();
  } else {
    saveStoredConfig();
  }

  _rebootPending = true;
  String ip = (_ip.length() > 0 && _ip != "dhcp") ? _ip : String("the DHCP-assigned IP");
  request->send(200, "text/plain",
                "Done. ESP will restart, connect to \"" + _ssid +
                "\" and go to IP address: " + ip);
}
