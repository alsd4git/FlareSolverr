import json
import logging
import os
import platform
import re
import shutil
import sys
import tempfile
import urllib.parse
import time
from typing import Optional

from selenium.webdriver.chrome.webdriver import WebDriver
import undetected_chromedriver as uc

FLARESOLVERR_VERSION = None
PLATFORM_VERSION = None
CHROME_EXE_PATH = None
CHROME_MAJOR_VERSION = None
USER_AGENT = None
XVFB_DISPLAY = None
PATCHED_DRIVER_PATH = None


def get_config_log_html() -> bool:
    return os.environ.get('LOG_HTML', 'false').lower() == 'true'


def get_config_headless() -> bool:
    return os.environ.get('HEADLESS', 'true').lower() == 'true'


def get_config_disable_media() -> bool:
    return os.environ.get('DISABLE_MEDIA', 'false').lower() == 'true'


def get_config_user_agent() -> Optional[str]:
    user_agent = os.environ.get('USER_AGENT', None)
    if user_agent is None:
        return None

    user_agent = user_agent.strip()
    if not user_agent:
        return None

    return user_agent


def get_config_cookie_jar_file() -> Optional[str]:
    cookie_jar_file = os.environ.get('COOKIE_JAR_FILE', None)
    if cookie_jar_file is None:
        return None

    cookie_jar_file = cookie_jar_file.strip()
    if not cookie_jar_file:
        return None

    return cookie_jar_file


def _parse_size(value: Optional[str]) -> Optional[tuple[int, int]]:
    if value is None:
        return None

    normalized_value = value.strip().lower().replace('x', ',')
    parts = [part.strip() for part in normalized_value.split(',') if part.strip()]
    if len(parts) != 2:
        return None

    try:
        width = int(parts[0])
        height = int(parts[1])
    except Exception:
        return None

    if width < 1 or height < 1:
        return None

    return width, height


def _parse_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None

    value = value.strip()
    if not value:
        return None

    try:
        parsed = float(value)
    except Exception:
        return None

    if parsed <= 0:
        return None

    return parsed


def _sanitize_locale_tag(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None

    value = value.strip()
    if not value:
        return None

    value = value.split('.', 1)[0]
    value = value.replace('_', '-')
    return value


def get_config_window_size() -> Optional[tuple[int, int]]:
    return _parse_size(os.environ.get('WINDOW_SIZE', None))


def get_config_screen_size() -> Optional[tuple[int, int]]:
    return _parse_size(os.environ.get('SCREEN_SIZE', None))


def get_config_device_scale_factor() -> Optional[float]:
    return _parse_float(os.environ.get('DEVICE_SCALE_FACTOR', None))


def get_config_browser_platform() -> Optional[str]:
    browser_platform = os.environ.get('BROWSER_PLATFORM', None)
    if browser_platform is None:
        return None

    browser_platform = browser_platform.strip()
    if not browser_platform:
        return None

    return browser_platform


def get_config_browser_locale() -> Optional[str]:
    browser_locale = os.environ.get('BROWSER_LOCALE', None)
    if browser_locale is None:
        browser_locale = _sanitize_locale_tag(os.environ.get('LANG', None))
    else:
        browser_locale = _sanitize_locale_tag(browser_locale)

    return browser_locale


def get_config_browser_languages() -> Optional[str]:
    browser_languages = os.environ.get('BROWSER_LANGUAGES', None)
    if browser_languages is None:
        browser_languages = _sanitize_locale_tag(os.environ.get('LANG', None))
    else:
        browser_languages = browser_languages.strip()

    if browser_languages is None:
        return None

    browser_languages = browser_languages.strip()
    if not browser_languages:
        return None

    return browser_languages


def _host_matches_cookie_domain(host: str, cookie_domain: str) -> bool:
    normalized_host = (host or '').lower()
    normalized_cookie_domain = (cookie_domain or '').lstrip('.').lower()

    if not normalized_host or not normalized_cookie_domain:
        return False

    return normalized_host == normalized_cookie_domain or normalized_host.endswith(
        '.' + normalized_cookie_domain
    )


def _cookie_target_host(cookie: dict) -> Optional[str]:
    cookie_url = cookie.get('url')
    if cookie_url:
        return urllib.parse.urlsplit(cookie_url).hostname

    cookie_domain = cookie.get('domain')
    if cookie_domain:
        return cookie_domain

    return None


def _load_cookie_jar() -> list[dict]:
    cookie_jar_file = get_config_cookie_jar_file()
    if cookie_jar_file is None:
        return []

    try:
        with open(cookie_jar_file, encoding='utf-8') as f:
            cookie_jar = json.load(f)
    except FileNotFoundError:
        logging.debug("Static cookie jar file not found: %s", cookie_jar_file)
        return []
    except Exception as e:
        logging.warning("Failed to load static cookie jar from %s: %s", cookie_jar_file, e)
        return []

    if isinstance(cookie_jar, dict):
        cookie_jar = cookie_jar.get('cookies', [])

    if not isinstance(cookie_jar, list):
        logging.warning("Static cookie jar must be a JSON array or an object with a 'cookies' array.")
        return []

    now = int(time.time())
    cookies = []
    for cookie in cookie_jar:
        if not isinstance(cookie, dict):
            continue

        expiry = cookie.get('expiry', cookie.get('expires'))
        if expiry is not None:
            try:
                if int(expiry) <= now:
                    continue
            except Exception:
                pass

        cookies.append(cookie)

    return cookies


def get_static_cookies_for_url(url: str) -> list[dict]:
    parsed_url = urllib.parse.urlsplit(url)
    host = parsed_url.hostname
    if host is None:
        return []

    static_cookies = []
    for cookie in _load_cookie_jar():
        cookie_host = _cookie_target_host(cookie)
        if cookie_host is None:
            continue
        if _host_matches_cookie_domain(host, cookie_host):
            static_cookies.append(cookie)

    return static_cookies


def normalize_request_headers(headers) -> dict:
    if headers is None:
        return {}

    if isinstance(headers, dict):
        items = headers.items()
    elif isinstance(headers, list):
        items = []
        for header in headers:
            if isinstance(header, dict):
                key = header.get('name', header.get('key'))
                value = header.get('value')
                if key is not None:
                    items.append((key, value))
            elif isinstance(header, (tuple, list)) and len(header) >= 2:
                items.append((header[0], header[1]))
    else:
        logging.debug("Unsupported legacy headers payload type: %s", type(headers).__name__)
        return {}

    normalized_headers = {}
    for key, value in items:
        if key is None or value is None:
            continue

        header_name = str(key).strip()
        if not header_name:
            continue

        if header_name.lower() in ('contenttype', 'content-type'):
            header_name = 'Content-Type'

        normalized_headers[header_name] = str(value)

    return normalized_headers


def get_flaresolverr_version() -> str:
    global FLARESOLVERR_VERSION
    if FLARESOLVERR_VERSION is not None:
        return FLARESOLVERR_VERSION

    package_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, 'package.json')
    if not os.path.isfile(package_path):
        package_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'package.json')
    with open(package_path) as f:
        FLARESOLVERR_VERSION = json.loads(f.read())['version']
        return FLARESOLVERR_VERSION

def get_current_platform() -> str:
    global PLATFORM_VERSION
    if PLATFORM_VERSION is not None:
        return PLATFORM_VERSION
    PLATFORM_VERSION = os.name
    return PLATFORM_VERSION


def create_proxy_extension(proxy: dict) -> str:
    parsed_url = urllib.parse.urlparse(proxy['url'])
    scheme = parsed_url.scheme
    host = parsed_url.hostname
    port = parsed_url.port
    username = proxy['username']
    password = proxy['password']
    manifest_json = """
    {
        "version": "1.0.0",
        "manifest_version": 3,
        "name": "Chrome Proxy",
        "permissions": [
            "proxy",
            "tabs",
            "storage",
            "webRequest",
            "webRequestAuthProvider"
        ],
        "host_permissions": [
          "<all_urls>"
        ],
        "background": {
          "service_worker": "background.js"
        },
        "minimum_chrome_version": "76.0.0"
    }
    """

    background_js = """
    var config = {
        mode: "fixed_servers",
        rules: {
            singleProxy: {
                scheme: "%s",
                host: "%s",
                port: %d
            },
            bypassList: ["localhost"]
        }
    };

    chrome.proxy.settings.set({value: config, scope: "regular"}, function() {});

    function callbackFn(details) {
        return {
            authCredentials: {
                username: "%s",
                password: "%s"
            }
        };
    }

    chrome.webRequest.onAuthRequired.addListener(
        callbackFn,
        { urls: ["<all_urls>"] },
        ['blocking']
    );
    """ % (
        scheme,
        host,
        port,
        username,
        password
    )

    proxy_extension_dir = tempfile.mkdtemp()

    with open(os.path.join(proxy_extension_dir, "manifest.json"), "w") as f:
        f.write(manifest_json)

    with open(os.path.join(proxy_extension_dir, "background.js"), "w") as f:
        f.write(background_js)

    return proxy_extension_dir


def get_webdriver(proxy: dict = None) -> WebDriver:
    global PATCHED_DRIVER_PATH, USER_AGENT
    logging.debug('Launching web browser...')

    # undetected_chromedriver
    options = uc.ChromeOptions()
    options.add_argument('--no-sandbox')
    window_size = get_config_window_size()
    if window_size is not None:
        options.add_argument(f'--window-size={window_size[0]},{window_size[1]}')
    else:
        options.add_argument('--window-size=1920,1080')
    options.add_argument('--disable-search-engine-choice-screen')
    # todo: this param shows a warning in chrome head-full
    options.add_argument('--disable-setuid-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    # this option removes the zygote sandbox (it seems that the resolution is a bit faster)
    options.add_argument('--no-zygote')
    # attempt to fix Docker ARM32 build
    IS_ARMARCH = platform.machine().startswith(('arm', 'aarch'))
    if IS_ARMARCH:
        options.add_argument('--disable-gpu-sandbox')
    options.add_argument('--ignore-certificate-errors')
    options.add_argument('--ignore-ssl-errors')

    browser_languages = get_config_browser_languages()
    if browser_languages is not None:
        options.add_argument('--accept-lang=%s' % browser_languages)
        options.add_argument('--lang=%s' % browser_languages.split(',', 1)[0])

    configured_user_agent = get_config_user_agent()
    if configured_user_agent is not None:
        options.add_argument('--user-agent=%s' % configured_user_agent)
    elif USER_AGENT is not None:
        # Fix for Chrome 117 | https://github.com/FlareSolverr/FlareSolverr/issues/910
        options.add_argument('--user-agent=%s' % USER_AGENT)

    proxy_extension_dir = None
    if proxy and all(key in proxy for key in ['url', 'username', 'password']):
        proxy_extension_dir = create_proxy_extension(proxy)
        options.add_argument("--disable-features=DisableLoadExtensionCommandLineSwitch")
        options.add_argument("--load-extension=%s" % os.path.abspath(proxy_extension_dir))
    elif proxy and 'url' in proxy:
        proxy_url = proxy['url']
        logging.debug("Using webdriver proxy: %s", proxy_url)
        options.add_argument('--proxy-server=%s' % proxy_url)

    # note: headless mode is detected (headless = True)
    # we launch the browser in head-full mode with the window hidden
    windows_headless = False
    if get_config_headless():
        if os.name == 'nt':
            windows_headless = True
        else:
            start_xvfb_display()
    # For normal headless mode:
    # options.add_argument('--headless')

    # if we are inside the Docker container, we avoid downloading the driver
    driver_exe_path = None
    version_main = None
    if os.path.exists("/app/chromedriver"):
        # running inside Docker
        driver_exe_path = "/app/chromedriver"
    else:
        version_main = get_chrome_major_version()
        if PATCHED_DRIVER_PATH is not None:
            driver_exe_path = PATCHED_DRIVER_PATH

    # detect chrome path
    browser_executable_path = get_chrome_exe_path()

    # downloads and patches the chromedriver
    # if we don't set driver_executable_path it downloads, patches, and deletes the driver each time
    try:
        driver = uc.Chrome(options=options, browser_executable_path=browser_executable_path,
                           driver_executable_path=driver_exe_path, version_main=version_main,
                           windows_headless=windows_headless, headless=get_config_headless())
    except Exception as e:
        logging.error("Error starting Chrome: %s" % e)
        # No point in continuing if we cannot retrieve the driver
        raise e

    # save the patched driver to avoid re-downloads
    if driver_exe_path is None:
        PATCHED_DRIVER_PATH = os.path.join(driver.patcher.data_path, driver.patcher.exe_name)
        if PATCHED_DRIVER_PATH != driver.patcher.executable_path:
            shutil.copy(driver.patcher.executable_path, PATCHED_DRIVER_PATH)

    apply_browser_fingerprint_overrides(driver)

    # clean up proxy extension directory
    if proxy_extension_dir is not None:
        shutil.rmtree(proxy_extension_dir)

    # selenium vanilla
    # options = webdriver.ChromeOptions()
    # options.add_argument('--no-sandbox')
    # options.add_argument('--window-size=1920,1080')
    # options.add_argument('--disable-setuid-sandbox')
    # options.add_argument('--disable-dev-shm-usage')
    # driver = webdriver.Chrome(options=options)

    return driver


def apply_browser_fingerprint_overrides(driver: WebDriver) -> None:
    configured_user_agent = get_config_user_agent()
    browser_platform = get_config_browser_platform()
    browser_locale = get_config_browser_locale()
    browser_languages = get_config_browser_languages()
    window_size = get_config_window_size()
    screen_size = get_config_screen_size() or window_size
    device_scale_factor = get_config_device_scale_factor()

    cdp_overrides = {}
    if configured_user_agent is not None:
        cdp_overrides["userAgent"] = configured_user_agent
    if browser_platform is not None:
        cdp_overrides["platform"] = browser_platform
    if browser_languages is not None:
        cdp_overrides["acceptLanguage"] = browser_languages

    if cdp_overrides:
        try:
            driver.execute_cdp_cmd("Emulation.setUserAgentOverride", cdp_overrides)
        except Exception as e:
            logging.debug("Failed to apply browser User-Agent override: %s", e)

    if window_size is not None:
        metrics = {
            "width": window_size[0],
            "height": window_size[1],
            "deviceScaleFactor": device_scale_factor if device_scale_factor is not None else 1,
            "mobile": False,
        }
        if screen_size is not None:
            metrics["screenWidth"] = screen_size[0]
            metrics["screenHeight"] = screen_size[1]
        try:
            driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", metrics)
            driver.set_window_size(window_size[0], window_size[1])
        except Exception as e:
            logging.debug("Failed to apply browser device metrics override: %s", e)

    if browser_locale is not None:
        try:
            driver.execute_cdp_cmd("Emulation.setLocaleOverride", {"locale": browser_locale})
        except Exception as e:
            logging.debug("Failed to apply browser locale override: %s", e)

    logging.debug(
        "Applied browser fingerprint overrides: %s",
        {
            "browserLocale": browser_locale,
            "browserLanguages": browser_languages,
            "browserPlatform": browser_platform,
            "deviceScaleFactor": device_scale_factor,
            "screenSize": screen_size,
            "windowSize": window_size,
        },
    )


def get_chrome_exe_path() -> str:
    global CHROME_EXE_PATH
    if CHROME_EXE_PATH is not None:
        return CHROME_EXE_PATH
    # linux pyinstaller bundle
    chrome_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'chrome', "chrome")
    if os.path.exists(chrome_path):
        if not os.access(chrome_path, os.X_OK):
            raise Exception(f'Chrome binary "{chrome_path}" is not executable. '
                            f'Please, extract the archive with "tar xzf <file.tar.gz>".')
        CHROME_EXE_PATH = chrome_path
        return CHROME_EXE_PATH
    # windows pyinstaller bundle
    chrome_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'chrome', "chrome.exe")
    if os.path.exists(chrome_path):
        CHROME_EXE_PATH = chrome_path
        return CHROME_EXE_PATH
    # system
    CHROME_EXE_PATH = uc.find_chrome_executable()
    return CHROME_EXE_PATH


def get_chrome_major_version() -> str:
    global CHROME_MAJOR_VERSION
    if CHROME_MAJOR_VERSION is not None:
        return CHROME_MAJOR_VERSION

    if os.name == 'nt':
        # Example: '104.0.5112.79'
        try:
            complete_version = extract_version_nt_executable(get_chrome_exe_path())
        except Exception:
            try:
                complete_version = extract_version_nt_registry()
            except Exception:
                # Example: '104.0.5112.79'
                complete_version = extract_version_nt_folder()
    else:
        chrome_path = get_chrome_exe_path()
        process = os.popen(f'"{chrome_path}" --version')
        # Example 1: 'Chromium 104.0.5112.79 Arch Linux\n'
        # Example 2: 'Google Chrome 104.0.5112.79 Arch Linux\n'
        complete_version = process.read()
        process.close()

    CHROME_MAJOR_VERSION = complete_version.split('.')[0].split(' ')[-1]
    return CHROME_MAJOR_VERSION


def extract_version_nt_executable(exe_path: str) -> str:
    import pefile
    pe = pefile.PE(exe_path, fast_load=True)
    pe.parse_data_directories(
        directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]]
    )
    return pe.FileInfo[0][0].StringTable[0].entries[b"FileVersion"].decode('utf-8')


def extract_version_nt_registry() -> str:
    stream = os.popen(
        'reg query "HKLM\\SOFTWARE\\Wow6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\Google Chrome"')
    output = stream.read()
    google_version = ''
    for letter in output[output.rindex('DisplayVersion    REG_SZ') + 24:]:
        if letter != '\n':
            google_version += letter
        else:
            break
    return google_version.strip()


def extract_version_nt_folder() -> str:
    # Check if the Chrome folder exists in the x32 or x64 Program Files folders.
    for i in range(2):
        path = 'C:\\Program Files' + (' (x86)' if i else '') + '\\Google\\Chrome\\Application'
        if os.path.isdir(path):
            paths = [f.path for f in os.scandir(path) if f.is_dir()]
            for path in paths:
                filename = os.path.basename(path)
                pattern = r'\d+\.\d+\.\d+\.\d+'
                match = re.search(pattern, filename)
                if match and match.group():
                    # Found a Chrome version.
                    return match.group(0)
    return ''


def get_user_agent(driver=None) -> str:
    global USER_AGENT
    configured_user_agent = get_config_user_agent()
    if driver is None:
        if USER_AGENT is not None:
            return USER_AGENT
        if configured_user_agent is not None:
            USER_AGENT = configured_user_agent
            return USER_AGENT

    if configured_user_agent is None and USER_AGENT is not None:
        return USER_AGENT

    owns_driver = driver is None
    try:
        if driver is None:
            driver = get_webdriver()
        USER_AGENT = driver.execute_script("return navigator.userAgent")
        # Fix for Chrome 117 | https://github.com/FlareSolverr/FlareSolverr/issues/910
        USER_AGENT = re.sub('HEADLESS', '', USER_AGENT, flags=re.IGNORECASE)
        if configured_user_agent is not None and USER_AGENT != configured_user_agent:
            logging.warning(
                "Configured USER_AGENT differs from the browser-reported value; "
                "using the browser-reported User-Agent."
            )
        return USER_AGENT
    except Exception as e:
        raise Exception("Error getting browser User-Agent. " + str(e))
    finally:
        if owns_driver and driver is not None:
            close_webdriver(driver)


def get_browser_fingerprint(driver: WebDriver) -> dict:
    return driver.execute_script("""
        return {
            userAgent: navigator.userAgent,
            userAgentData: navigator.userAgentData ? {
                brands: navigator.userAgentData.brands,
                mobile: navigator.userAgentData.mobile,
                platform: navigator.userAgentData.platform
            } : null,
            platform: navigator.platform,
            vendor: navigator.vendor,
            languages: navigator.languages,
            language: navigator.language,
            webdriver: navigator.webdriver,
            devicePixelRatio: window.devicePixelRatio,
            screen: {
                width: window.screen.width,
                height: window.screen.height,
                availWidth: window.screen.availWidth,
                availHeight: window.screen.availHeight
            },
            viewport: {
                width: window.innerWidth,
                height: window.innerHeight
            },
            hardwareConcurrency: navigator.hardwareConcurrency,
            deviceMemory: navigator.deviceMemory,
            maxTouchPoints: navigator.maxTouchPoints,
            timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
            locale: Intl.DateTimeFormat().resolvedOptions().locale,
            connection: navigator.connection ? navigator.connection.effectiveType : null
        };
    """)


def log_browser_fingerprint(driver: WebDriver) -> None:
    fingerprint = get_browser_fingerprint(driver)
    logging.info("Browser fingerprint: %s", json.dumps(fingerprint, ensure_ascii=False, sort_keys=True))


def close_webdriver(driver: WebDriver) -> None:
    if PLATFORM_VERSION == "nt":
        driver.close()
    driver.quit()


def start_xvfb_display():
    global XVFB_DISPLAY
    if XVFB_DISPLAY is None:
        from xvfbwrapper import Xvfb
        screen_size = get_config_screen_size() or get_config_window_size()
        if screen_size is None:
            XVFB_DISPLAY = Xvfb()
        else:
            XVFB_DISPLAY = Xvfb(width=screen_size[0], height=screen_size[1])
        XVFB_DISPLAY.start()


def object_to_dict(_object):
    json_dict = json.loads(json.dumps(_object, default=lambda o: o.__dict__))
    # remove hidden fields
    return {k: v for k, v in json_dict.items() if not k.startswith('__')}


def redact_sensitive_data(value, parent_key: Optional[str] = None):
    if isinstance(value, dict):
        redacted = {}
        for key, child_value in value.items():
            normalized_key = str(key).lower()
            if normalized_key in ('password', 'postdata', 'turnstile_token'):
                redacted[key] = '[REDACTED]'
            elif normalized_key == 'value' and parent_key == 'cookies':
                redacted[key] = '[REDACTED]'
            elif normalized_key == 'proxy' and isinstance(child_value, dict):
                redacted[key] = redact_sensitive_data(child_value, normalized_key)
            else:
                redacted[key] = redact_sensitive_data(child_value, normalized_key)
        return redacted

    if isinstance(value, list):
        return [redact_sensitive_data(item, parent_key) for item in value]

    return value
