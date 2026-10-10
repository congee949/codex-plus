#include <node_api.h>
#include <Security/Security.h>
#include <bsm/libbsm.h>
#include <libproc.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#include <limits.h>
#include <string>

static napi_value Result(napi_env env, bool allowed, const char* reason) {
  napi_value object, flag, text;
  napi_create_object(env, &object);
  napi_get_boolean(env, allowed, &flag);
  napi_create_string_utf8(env, reason, NAPI_AUTO_LENGTH, &text);
  napi_set_named_property(env, object, "authorized", flag);
  napi_set_named_property(env, object, "reason", text);
  return object;
}

static bool Info(pid_t pid, proc_bsdinfo& info) {
  return proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, sizeof(info)) == sizeof(info);
}

static bool Same(const proc_bsdinfo& a, const proc_bsdinfo& b) {
  return a.pbi_pid == b.pbi_pid && a.pbi_ppid == b.pbi_ppid &&
         a.pbi_start_tvsec == b.pbi_start_tvsec &&
         a.pbi_start_tvusec == b.pbi_start_tvusec;
}

static bool PathMatches(pid_t pid, const std::string& expected) {
  char actual[PROC_PIDPATHINFO_MAXSIZE], canonical[PATH_MAX];
  return !expected.empty() && realpath(expected.c_str(), canonical) &&
         proc_pidpath(pid, actual, sizeof(actual)) > 0 &&
         std::string(actual) == canonical;
}

static bool Equals(CFTypeRef value, CFStringRef expected) {
  return value && CFGetTypeID(value) == CFStringGetTypeID() &&
         CFStringCompare(static_cast<CFStringRef>(value), expected, 0) == kCFCompareEqualTo;
}

static bool Trusted(CFDictionaryRef attributes, CFStringRef identifier) {
  SecCodeRef code = nullptr;
  if (SecCodeCopyGuestWithAttributes(nullptr, attributes, kSecCSDefaultFlags, &code)
      != errSecSuccess) return false;
  CFDictionaryRef information = nullptr;
  bool valid = SecCodeCheckValidity(code, kSecCSStrictValidate, nullptr) == errSecSuccess &&
      SecCodeCopySigningInformation(code, kSecCSSigningInformation, &information) == errSecSuccess;
  if (valid) {
    valid = Equals(CFDictionaryGetValue(information, kSecCodeInfoTeamIdentifier),
                   CFSTR("2DC432GLL2")) &&
            Equals(CFDictionaryGetValue(information, kSecCodeInfoIdentifier), identifier);
  }
  if (information) CFRelease(information);
  CFRelease(code);
  return valid;
}

static std::string CurrentBundle() {
  const char* fixture = getenv("CODEX_LOCAL_APP_TOOLS_FIXTURE_BUNDLE");
  std::string path;
  if (fixture && *fixture) {
    path = fixture;
  } else {
    char actual[PROC_PIDPATHINFO_MAXSIZE];
    if (proc_pidpath(getpid(), actual, sizeof(actual)) <= 0) return {};
    path = actual;
  }
  std::string marker = "/Contents/MacOS/";
  auto pos = path.rfind(marker);
  return pos == std::string::npos ? std::string() : path.substr(0, pos);
}

static napi_value Authorize(napi_env env, napi_callback_info callback) {
  napi_value arguments[1];
  size_t count = 1;
  napi_get_cb_info(env, callback, &count, arguments, nullptr, nullptr);
  int32_t fd = -1;
  if (count != 1 || napi_get_value_int32(env, arguments[0], &fd) != napi_ok || fd < 0)
    return Result(env, false, "local-invalid-arguments");
  std::string bundle = CurrentBundle();
  if (bundle.empty()) return Result(env, false, "local-untrusted-executable-path");
  std::string nodePath = bundle + "/Contents/Resources/cua_node/bin/node";
  std::string cliPath = bundle + "/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex";
  audit_token_t token{};
  socklen_t length = sizeof(token);
  if (getsockopt(fd, SOL_LOCAL, LOCAL_PEERTOKEN, &token, &length) != 0 ||
      length != sizeof(token) || audit_token_to_euid(token) != geteuid())
    return Result(env, false, "local-invalid-peer-token");
  pid_t peer = audit_token_to_pid(token);
  proc_bsdinfo before{}, parent{}, after{}, parentAfter{};
  if (!Info(peer, before) || !Info(before.pbi_ppid, parent) ||
      parent.pbi_ppid != static_cast<uint32_t>(getpid()) ||
      before.pbi_uid != geteuid() || parent.pbi_uid != geteuid())
    return Result(env, false, "local-untrusted-process-chain");
  if (!PathMatches(peer, nodePath) || !PathMatches(parent.pbi_pid, cliPath))
    return Result(env, false, "local-untrusted-executable-path");

  // The audit token binds the peer to a process instance; verify the parent
  // before and after checking signatures to reject a changing process chain.
  CFDataRef audit = CFDataCreate(nullptr, reinterpret_cast<const UInt8*>(&token), sizeof(token));
  const void* auditKeys[] = {kSecGuestAttributeAudit};
  const void* auditValues[] = {audit};
  CFDictionaryRef peerAttributes = CFDictionaryCreate(nullptr, auditKeys, auditValues, 1,
      &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
  int parentPid = parent.pbi_pid;
  CFNumberRef pid = CFNumberCreate(nullptr, kCFNumberIntType, &parentPid);
  const void* pidKeys[] = {kSecGuestAttributePid};
  const void* pidValues[] = {pid};
  CFDictionaryRef parentAttributes = CFDictionaryCreate(nullptr, pidKeys, pidValues, 1,
      &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
  bool trusted = Trusted(peerAttributes, CFSTR("node")) &&
                 Trusted(parentAttributes, CFSTR("codex"));
  CFRelease(parentAttributes);
  CFRelease(pid);
  CFRelease(peerAttributes);
  CFRelease(audit);
  if (!trusted) return Result(env, false, "local-untrusted-child-signature");
  if (!Info(peer, after) || !Info(parent.pbi_pid, parentAfter) ||
      !Same(before, after) || !Same(parent, parentAfter))
    return Result(env, false, "local-process-chain-changed");
  return Result(env, true, "local-app-signed-child");
}

static napi_value Initialize(napi_env env, napi_value exports) {
  napi_value function;
  napi_create_function(env, "authorizeLocalChild", NAPI_AUTO_LENGTH, Authorize, nullptr, &function);
  napi_set_named_property(env, exports, "authorizeLocalChild", function);
  return exports;
}

NAPI_MODULE(NODE_GYP_MODULE_NAME, Initialize)
