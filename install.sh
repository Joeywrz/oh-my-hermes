#!/usr/bin/env sh
set -eu

OMH_REPO_ARCHIVE_ROOT="${OMH_REPO_ARCHIVE_ROOT:-https://github.com/rlaope/oh-my-hermes/archive/refs}"
OMH_REPO_ASSET_ROOT="${OMH_REPO_ASSET_ROOT:-https://github.com/rlaope/oh-my-hermes/releases/download}"
OMH_REPO_LATEST_URL="${OMH_REPO_LATEST_URL:-https://github.com/rlaope/oh-my-hermes/releases/latest}"
# Stable installs the ~2.7 MB release wheel; preview installs the ~44 MB branch
# archive, which GitHub generates per request rather than serving from a CDN.
# Defaulting to the newest release is the contract every package manager
# honours; preview stays available for tracking main.
OMH_CHANNEL="${OMH_CHANNEL:-stable}"
OMH_VERSION="${OMH_VERSION:-}"
OMH_PACKAGE_URL="${OMH_PACKAGE_URL:-}"
OMH_SOURCE_REF="${OMH_SOURCE_REF:-}"
OMH_PYTHON_WAS_SET="${OMH_PYTHON:+x}"
OMH_PYTHON="${OMH_PYTHON:-python3}"
OMH_PIP_ARGS_WAS_SET="${OMH_PIP_ARGS+x}"
OMH_PIP_ARGS="${OMH_PIP_ARGS:-}"
OMH_INSTALL_MODE="${OMH_INSTALL_MODE:-venv}"
OMH_HOME_DIR="${HOME:-}"
if [ -z "${OMH_VENV_DIR+x}" ]; then
  if [ -n "${XDG_DATA_HOME:-}" ]; then
    OMH_VENV_DIR="$XDG_DATA_HOME/omh/venv"
  elif [ -n "$OMH_HOME_DIR" ]; then
    OMH_VENV_DIR="$OMH_HOME_DIR/.local/share/omh/venv"
  else
    OMH_VENV_DIR=""
  fi
fi
if [ -z "${OMH_BIN_DIR+x}" ]; then
  if [ -n "$OMH_HOME_DIR" ]; then
    OMH_BIN_DIR="$OMH_HOME_DIR/.local/bin"
  else
    OMH_BIN_DIR=""
  fi
fi
OMH_LINK_COMMAND="${OMH_LINK_COMMAND:-1}"
OMH_FORCE_LINK="${OMH_FORCE_LINK:-0}"
OMH_RUN_SETUP="${OMH_RUN_SETUP:-0}"
OMH_AUTO_APPLY="${OMH_AUTO_APPLY:-1}"
OMH_RUN_DOCTOR="${OMH_RUN_DOCTOR:-1}"
OMH_WITH_PLUGIN="${OMH_WITH_PLUGIN:-0}"
OMH_WITH_MCP="${OMH_WITH_MCP:-0}"
OMH_PROFILE_PACKS="${OMH_PROFILE_PACKS:-}"
OMH_SETUP_PROFILES="${OMH_SETUP_PROFILES:-}"
OMH_DEFAULT_EXECUTOR="${OMH_DEFAULT_EXECUTOR:-}"
OMH_SCOPE="${OMH_SCOPE:-}"
OMH_SETUP_ARGS="${OMH_SETUP_ARGS:-}"
OMH_LANG_RAW="${OMH_LANG:-${OMH_LANGUAGE:-}}"
OMH_LANG_WAS_SET=0
if [ -n "$OMH_LANG_RAW" ]; then
  OMH_LANG_WAS_SET=1
fi
OMH_RUNTIME_PYTHON="$OMH_PYTHON"
OMH_COMMAND_HINT=""
OMH_INSTALL_STEP_COUNT=2
if [ "$OMH_INSTALL_MODE" = "python" ]; then
  OMH_INSTALL_STEP_COUNT=1
fi
OMH_EXPOSE_STEP=$((OMH_INSTALL_STEP_COUNT + 1))
OMH_SETUP_STEP=$((OMH_EXPOSE_STEP + 1))
OMH_DOCTOR_STEP=$((OMH_SETUP_STEP + 1))
OMH_TOTAL_STEPS=$OMH_EXPOSE_STEP
if [ "$OMH_RUN_SETUP" = "1" ]; then
  OMH_TOTAL_STEPS=$OMH_DOCTOR_STEP
fi

say() {
  printf '%s\n' "$*"
}

use_color() {
  [ -t 1 ] && [ -z "${NO_COLOR:-}" ]
}

color() {
  if use_color; then
    printf '\033[%sm%s\033[0m' "$1" "$2"
  else
    printf '%s' "$2"
  fi
}

say_header() {
  printf '%s\n' "$(color '1;36' "$1")"
  if [ -n "${2:-}" ]; then
    printf '%s\n' "$2"
  fi
  printf '\n'
}

say_step() {
  printf '%s %s\n' "$(color '1;36' "$1")" "$2"
}

say_ok() {
  printf '      %s %s\n' "$(color '1;32' '[ok]')" "$1"
}

say_note() {
  printf '      %s %s\n' "$(color '1;33' '[note]')" "$1"
}

say_fail() {
  printf '      %s %s\n' "$(color '1;31' '[failed]')" "$1"
}

step_label() {
  printf '[%s/%s]' "$1" "$OMH_TOTAL_STEPS"
}

normalize_omh_lang() {
  OMH_LANG_KEY="$(printf '%s' "$1" | tr 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' 'abcdefghijklmnopqrstuvwxyz')"
  case "$OMH_LANG_KEY" in
    ""|en|eng|english)
      printf 'en'
      ;;
    ko|kr|kor|korean)
      printf 'ko'
      ;;
    ja|jp|jpn|japanese)
      printf 'ja'
      ;;
    zh|cn|zho|chi|chinese)
      printf 'zh'
      ;;
    *)
      printf 'omh installer: unsupported OMH_LANG %s (expected en, ko, ja, or zh).\n' "$1" >&2
      exit 1
      ;;
  esac
}

OMH_LANG="$(normalize_omh_lang "$OMH_LANG_RAW")"

msg() {
  case "$OMH_LANG:$1" in
    ko:installer_title) printf 'OMH 설치 관리자' ;;
    ko:installer_subtitle) printf '시스템 Python 패키지를 건드리지 않고 oh-my-hermes를 설치합니다.' ;;
    ko:channel) printf '채널' ;;
    ko:mode) printf '모드' ;;
    ko:step_create_venv) printf '격리된 Python 환경 생성:' ;;
    ko:step_install_package) printf 'OMH 패키지 설치' ;;
    ko:step_install_python) printf '선택한 Python에 OMH 패키지 설치' ;;
    ko:step_expose_command) printf 'omh 명령어 연결' ;;
    ko:step_setup) printf '관리 Hermes 스킬 설정(명시적 선택)' ;;
    ko:step_doctor) printf '설치 확인' ;;
    ko:done) printf '완료' ;;
    ko:installed) printf 'oh-my-hermes 설치가 완료되었습니다.' ;;
    ko:next_path) printf '다음: '\''omh setup'\''으로 Hermes에 연결한 뒤, '\''omh doctor'\''로 확인하세요.' ;;
    ko:next_command_path) printf '다음: '\''%s setup'\''으로 Hermes에 연결한 뒤, '\''%s doctor'\''로 확인하세요. 또는 해당 디렉터리를 PATH에 추가하세요.' "$2" "$2" ;;
    ja:installer_title) printf 'OMH インストーラー' ;;
    ja:installer_subtitle) printf 'システム Python パッケージを変更せずに oh-my-hermes をインストールします。' ;;
    ja:channel) printf 'チャンネル' ;;
    ja:mode) printf 'モード' ;;
    ja:step_create_venv) printf '分離された Python 環境を作成:' ;;
    ja:step_install_package) printf 'OMH パッケージをインストール' ;;
    ja:step_install_python) printf '選択した Python に OMH パッケージをインストール' ;;
    ja:step_expose_command) printf 'omh コマンドを公開' ;;
    ja:step_setup) printf '管理 Hermes スキルを設定（明示的に選択）' ;;
    ja:step_doctor) printf 'インストールを検証' ;;
    ja:done) printf '完了' ;;
    ja:installed) printf 'oh-my-hermes のインストールが完了しました。' ;;
    ja:next_path) printf '次に '\''omh setup'\'' で Hermes に接続し、'\''omh doctor'\'' で確認してください。' ;;
    ja:next_command_path) printf '次に '\''%s setup'\'' で Hermes に接続し、'\''%s doctor'\'' で確認してください。または、そのディレクトリを PATH に追加してください。' "$2" "$2" ;;
    zh:installer_title) printf 'OMH 安装器' ;;
    zh:installer_subtitle) printf '在不改动系统 Python 包的情况下安装 oh-my-hermes。' ;;
    zh:channel) printf '频道' ;;
    zh:mode) printf '模式' ;;
    zh:step_create_venv) printf '创建隔离 Python 环境:' ;;
    zh:step_install_package) printf '安装 OMH 包' ;;
    zh:step_install_python) printf '将 OMH 包安装到所选 Python' ;;
    zh:step_expose_command) printf '公开 omh 命令' ;;
    zh:step_setup) printf '设置托管 Hermes 技能（显式选择）' ;;
    zh:step_doctor) printf '验证安装' ;;
    zh:done) printf '完成' ;;
    zh:installed) printf 'oh-my-hermes 已安装。' ;;
    zh:next_path) printf '下一步：运行 '\''omh setup'\'' 连接 Hermes，然后运行 '\''omh doctor'\'' 验证。' ;;
    zh:next_command_path) printf '下一步：运行 '\''%s setup'\'' 连接 Hermes，然后运行 '\''%s doctor'\'' 验证；也可以把该目录加入 PATH。' "$2" "$2" ;;
    *)
      case "$1" in
        installer_title) printf 'OMH installer' ;;
        installer_subtitle) printf 'Install oh-my-hermes without touching system Python packages.' ;;
        channel) printf 'Channel' ;;
        mode) printf 'Mode' ;;
        step_create_venv) printf 'Create isolated Python environment at' ;;
        step_install_package) printf 'Install OMH package' ;;
        step_install_python) printf 'Install OMH package into selected Python' ;;
        step_expose_command) printf 'Expose the omh command' ;;
        step_setup) printf 'Set up managed Hermes skills (explicit opt-in)' ;;
        step_doctor) printf 'Verify installation' ;;
        done) printf 'done' ;;
        installed) printf 'oh-my-hermes is installed.' ;;
        next_path) printf 'Next: run '\''omh setup'\'' to connect OMH to Hermes, then '\''omh doctor'\'' to verify.' ;;
        next_command_path) printf 'Next: run '\''%s setup'\'' to connect OMH to Hermes, then '\''%s doctor'\'' to verify, or add its directory to PATH.' "$2" "$2" ;;
        *) printf '%s' "$1" ;;
      esac
      ;;
  esac
}

run_step() {
  OMH_STEP_PREFIX="$1"
  OMH_STEP_LABEL="$2"
  shift 2
  say_step "$OMH_STEP_PREFIX" "$OMH_STEP_LABEL"
  if OMH_STEP_OUTPUT="$("$@" 2>&1)"; then
    say_ok "$(msg done)"
    return 0
  fi
  say_fail "$OMH_STEP_LABEL"
  if [ -n "$OMH_STEP_OUTPUT" ]; then
    printf '%s\n' "$OMH_STEP_OUTPUT" | sed 's/^/      /'
  fi
  exit 1
}

run_omh() {
  "$OMH_RUNTIME_PYTHON" -m omh.cli "$@"
}

find_omh_command() {
  if [ -n "$OMH_COMMAND_HINT" ] && [ -e "$OMH_COMMAND_HINT" ]; then
    printf '%s\n' "$OMH_COMMAND_HINT"
    return 0
  fi
  if command -v omh >/dev/null 2>&1; then
    command -v omh
    return 0
  fi
  "$OMH_RUNTIME_PYTHON" - "$OMH_BIN_DIR" "$OMH_VENV_DIR/bin" <<'PY'
import os
import shutil
import site
import sys
import sysconfig

found = shutil.which("omh")
if found:
    print(found)
    raise SystemExit(0)

names = ["omh.exe"] if os.name == "nt" else ["omh"]
schemes = [sysconfig.get_default_scheme()]
schemes.append("nt_user" if os.name == "nt" else "posix_user")

dirs = []
for directory in sys.argv[1:]:
    if directory and directory not in dirs:
        dirs.append(directory)

for scheme in schemes:
    try:
        path = sysconfig.get_path("scripts", scheme)
    except Exception:
        path = ""
    if path and path not in dirs:
        dirs.append(path)

user_base = getattr(site, "USER_BASE", "")
if user_base:
    user_bin = os.path.join(user_base, "Scripts" if os.name == "nt" else "bin")
    if user_bin not in dirs:
        dirs.append(user_bin)

for directory in dirs:
    for name in names:
        candidate = os.path.join(directory, name)
        if os.path.exists(candidate):
            print(candidate)
            raise SystemExit(0)
PY
}

link_omh_command() {
  if [ "$OMH_LINK_COMMAND" != "1" ]; then
    return 0
  fi
  if [ -z "$OMH_BIN_DIR" ]; then
    say "omh installer: OMH_BIN_DIR is not set, so no omh command link was created."
    return 0
  fi
  OMH_SOURCE_COMMAND="$OMH_VENV_DIR/bin/omh"
  if [ ! -e "$OMH_SOURCE_COMMAND" ]; then
    return 0
  fi
  mkdir -p "$OMH_BIN_DIR"
  OMH_TARGET_COMMAND="$OMH_BIN_DIR/omh"
  if [ -e "$OMH_TARGET_COMMAND" ] || [ -L "$OMH_TARGET_COMMAND" ]; then
    OMH_EXISTING_LINK="$(readlink "$OMH_TARGET_COMMAND" 2>/dev/null || true)"
    if [ "$OMH_EXISTING_LINK" = "$OMH_SOURCE_COMMAND" ]; then
      OMH_COMMAND_HINT="$OMH_TARGET_COMMAND"
      return 0
    fi
    if [ "$OMH_FORCE_LINK" = "1" ]; then
      ln -sf "$OMH_SOURCE_COMMAND" "$OMH_TARGET_COMMAND"
      OMH_COMMAND_HINT="$OMH_TARGET_COMMAND"
      return 0
    fi
    say "omh installer: $OMH_TARGET_COMMAND already exists, so it was not replaced."
    say "Set OMH_FORCE_LINK=1 to replace it, or use: $OMH_SOURCE_COMMAND"
    OMH_COMMAND_HINT="$OMH_SOURCE_COMMAND"
    return 0
  fi
  ln -s "$OMH_SOURCE_COMMAND" "$OMH_TARGET_COMMAND"
  OMH_COMMAND_HINT="$OMH_TARGET_COMMAND"
}

install_into_venv() {
  if [ -z "$OMH_VENV_DIR" ]; then
    say "omh installer: HOME or XDG_DATA_HOME is required for default venv install."
    say "Set OMH_VENV_DIR to an explicit directory and retry."
    exit 1
  fi
  run_step "$(step_label 1)" "$(msg step_create_venv) $OMH_VENV_DIR" "$OMH_PYTHON" -m venv "$OMH_VENV_DIR"
  OMH_RUNTIME_PYTHON="$OMH_VENV_DIR/bin/python"
  run_step "$(step_label 2)" "$(msg step_install_package)" sh -c '
    # Intentional shell splitting: OMH_PIP_ARGS is an advanced operator escape hatch.
    # shellcheck disable=SC2086
    PIP_DISABLE_PIP_VERSION_CHECK=1 "$1" -m pip install --disable-pip-version-check -q --no-cache-dir --force-reinstall $2 --upgrade "$3"
  ' sh "$OMH_RUNTIME_PYTHON" "$OMH_PIP_ARGS" "$OMH_PACKAGE_URL"
  OMH_COMMAND_HINT="$OMH_VENV_DIR/bin/omh"
  link_omh_command
}

install_into_python() {
  OMH_DIRECT_PIP_ARGS="$OMH_PIP_ARGS"
  if [ -z "$OMH_PIP_ARGS_WAS_SET" ]; then
    OMH_DIRECT_PIP_ARGS="--user"
  fi
  run_step "$(step_label 1)" "$(msg step_install_python)" sh -c '
    # Intentional shell splitting: OMH_DIRECT_PIP_ARGS is an advanced operator escape hatch.
    # shellcheck disable=SC2086
    PIP_DISABLE_PIP_VERSION_CHECK=1 "$1" -m pip install --disable-pip-version-check -q --no-cache-dir --force-reinstall $2 --upgrade "$3"
  ' sh "$OMH_PYTHON" "$OMH_DIRECT_PIP_ARGS" "$OMH_PACKAGE_URL"
  OMH_RUNTIME_PYTHON="$OMH_PYTHON"
}

# The floor the wheel itself declares (`requires-python` in pyproject.toml).
# Raising one without the other turns a refusal a person can act on into a pip
# error several steps later.
OMH_MIN_PYTHON_MINOR=11
# What `uv python install` is asked for when it is the last resort. A concrete
# version rather than a range: uv resolves a range against what it already has,
# and the point of reaching here is that nothing on this machine qualifies.
OMH_UV_PYTHON_VERSION=3.12
# Declines the interpreter download; any other value leaves it available.
OMH_PROVISION_PYTHON="${OMH_PROVISION_PYTHON:-1}"
# Set when provisioning ran and still did not produce an interpreter, so the
# refusal can say that instead of repeating "none was found".
OMH_PROVISION_TRIED=0
OMH_INSTALL_SCRIPT_URL="https://raw.githubusercontent.com/rlaope/oh-my-hermes/main/install.sh"

python_two_part_version() {
  # Anchored on a whole line, because this is matched against arbitrary
  # interpreter output and a wrapper that prints a banner first would
  # otherwise satisfy it. `-I` keeps a stray PYTHONPATH or a user
  # site-packages out of the answer. A candidate that is not installed, or is
  # the Microsoft Store stub, prints nothing and is rejected by the emptiness.
  "$1" -I -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null |
    awk '
      NR == 1 && /^[0-9]+\.[0-9]+$/ { version = $0 }
      END { if (NR == 1 && version != "") print version }'
}

python_version_supported() {
  [ -n "$1" ] || return 1
  printf '%s' "$1" | awk -F. -v minor="$OMH_MIN_PYTHON_MINOR" '
    { exit !($1 > 3 || ($1 == 3 && $2 >= minor)) }'
}

python_candidates() {
  # An interpreter the operator named is the only candidate. Quietly using a
  # different one would be the installer overruling a choice, and the reason
  # OMH_PYTHON exists is that the person knows something the search does not.
  if [ -n "$OMH_PYTHON_WAS_SET" ]; then
    printf '%s\n' "$OMH_PYTHON"
    return 0
  fi
  # The machine's own python3 first, then versioned commands newest-first:
  # the same order packaging/npm/lib/python.js probes, so the curl installer
  # and the npm launcher land on the same interpreter on one machine.
  printf '%s\n' python3 python3.14 python3.13 python3.12 python3.11 python
  # Interpreters a person installed and did not put first on PATH. This is the
  # case that produced the report: on macOS /usr/bin/python3 is 3.9 and stays
  # ahead of everything, while the Homebrew or python.org build is one
  # directory away. Order among these is not a preference -- every interpreter
  # that passes the probe runs OMH equally well -- so they are only places to
  # look, and the probe decides.
  for omh_python_candidate in \
    /opt/homebrew/bin/python3.[0-9] \
    /opt/homebrew/bin/python3.[0-9][0-9] \
    /usr/local/bin/python3.[0-9] \
    /usr/local/bin/python3.[0-9][0-9] \
    /Library/Frameworks/Python.framework/Versions/*/bin/python3 \
    "$OMH_HOME_DIR"/.pyenv/versions/*/bin/python3 \
    "$OMH_HOME_DIR"/.local/share/uv/python/*/bin/python3
  do
    if [ -x "$omh_python_candidate" ]; then
      printf '%s\n' "$omh_python_candidate"
    fi
  done
  # uv's managed interpreters, asked for by uv rather than assumed. The line
  # above covers uv's default location only, and `UV_PYTHON_INSTALL_DIR` or
  # `XDG_DATA_HOME` moves it -- on such a machine a perfectly good interpreter
  # sits one directory away and the search would report none. `uv python dir`
  # answers the same from any working directory; `uv python find` does NOT and
  # must not be used here, because it resolves a PROJECT environment first and
  # `curl ... | sh` runs wherever the person is standing.
  if command -v uv >/dev/null 2>&1; then
    omh_uv_root="$(uv python dir 2>/dev/null | tail -1)"
    if [ -n "$omh_uv_root" ] && [ -d "$omh_uv_root" ]; then
      for omh_python_candidate in "$omh_uv_root"/*/bin/python3 "$omh_uv_root"/*/bin/python3.[0-9] "$omh_uv_root"/*/bin/python3.[0-9][0-9]; do
        if [ -x "$omh_python_candidate" ]; then
          printf '%s\n' "$omh_python_candidate"
        fi
      done
    fi
  fi
}

select_python() {
  python_candidates | while IFS= read -r omh_python_candidate; do
    if python_version_supported "$(python_two_part_version "$omh_python_candidate")"; then
      printf '%s\n' "$omh_python_candidate"
      break
    fi
  done
}

report_rejected_pythons() {
  # Only interpreters that exist and answered. A candidate name that is not
  # installed at all is not a finding, and listing it would bury the one line
  # that explains the refusal.
  python_candidates | while IFS= read -r omh_python_candidate; do
    omh_python_version="$(python_two_part_version "$omh_python_candidate")"
    if [ -n "$omh_python_version" ]; then
      say "  $omh_python_candidate is Python $omh_python_version"
    fi
  done
}


resolve_python() {
  # Runs after the header so its one long case -- provisioning an
  # interpreter -- is announced under the same banner as every other step,
  # and so the refusal is the last thing on screen when there is one.
  OMH_SELECTED_PYTHON="$(select_python)"

  # Last resort, and only when the alternative is failing: uv is the person's own
  # tool and it installs a private interpreter under their data directory. It is
  # still a download of a language runtime, which is a kind of side effect this
  # script has never had -- until now it fetched only OMH's own wheel -- so it
  # says what it is about to do, shows uv's own output while it does it, and
  # `OMH_PROVISION_PYTHON=0` declines it without having to name an interpreter.
  if [ -z "$OMH_SELECTED_PYTHON" ] \
    && [ -z "$OMH_PYTHON_WAS_SET" ] \
    && [ "$OMH_PROVISION_PYTHON" != "0" ] \
    && command -v uv >/dev/null 2>&1; then
    say_note "No Python 3.$OMH_MIN_PYTHON_MINOR+ was found. Asking uv, which is already on this machine, for Python $OMH_UV_PYTHON_VERSION."
    say_note "This downloads an interpreter (~66 MB). Decline with OMH_PROVISION_PYTHON=0."
    if uv python install "$OMH_UV_PYTHON_VERSION"; then
      # Deliberately NOT `uv python find`: it resolves a PROJECT environment
      # before it considers uv's own interpreters and is cwd-dependent, and
      # `curl ... | sh` runs wherever the person is standing. From a checkout
      # with a `.venv` it answers that `.venv` -- which reports a supported
      # version, passes the probe, and would become the BASE of OMH's venv,
      # leaving it pointing at an interpreter that vanishes when that project
      # is rebuilt. `--managed-python` does not reliably prevent it either:
      # measured on uv 0.12.5, both forms still answered with the project's
      # `.venv`. So the search runs again instead, and it now reads uv's
      # managed directory from `uv python dir`, which is cwd-independent.
      OMH_SELECTED_PYTHON="$(select_python)"
      if [ -z "$OMH_SELECTED_PYTHON" ]; then
        OMH_PROVISION_TRIED=1
      fi
    else
      OMH_PROVISION_TRIED=1
    fi
  fi

  if [ -z "$OMH_SELECTED_PYTHON" ]; then
    if [ -n "$OMH_PYTHON_WAS_SET" ] && ! command -v "$OMH_PYTHON" >/dev/null 2>&1; then
      # A typo is the likeliest way this variable goes wrong, and "not a usable
      # Python 3.11+" would send the person to look at the wrong thing.
      say "omh installer: OMH_PYTHON='$OMH_PYTHON' was not found."
    elif [ -n "$OMH_PYTHON_WAS_SET" ]; then
      say "omh installer: OMH_PYTHON='$OMH_PYTHON' is not a usable Python 3.$OMH_MIN_PYTHON_MINOR+."
    elif [ "$OMH_PROVISION_TRIED" = "1" ]; then
      say "omh installer: no Python 3.$OMH_MIN_PYTHON_MINOR+ was found, and asking uv for one did not produce a usable interpreter (uv's own output is above)."
    else
      say "omh installer: no Python 3.$OMH_MIN_PYTHON_MINOR+ was found, and oh-my-hermes requires one."
    fi
    report_rejected_pythons
    say "Install one, then run this installer again:"
    case "$(uname -s 2>/dev/null || printf 'unknown')" in
      Darwin) say "  brew install python@$OMH_UV_PYTHON_VERSION   (or https://www.python.org/downloads/)" ;;
      Linux) say "  your distribution's python3.$OMH_MIN_PYTHON_MINOR+ package, or https://www.python.org/downloads/" ;;
      *) say "  https://www.python.org/downloads/" ;;
    esac
    if [ -z "$OMH_PYTHON_WAS_SET" ]; then
      say "Already have one the search did not look in? Name it:"
      say "  OMH_PYTHON=/path/to/python$OMH_UV_PYTHON_VERSION sh -c \"\$(curl -fsSL $OMH_INSTALL_SCRIPT_URL)\""
    else
      say "Or point OMH_PYTHON at a Python 3.$OMH_MIN_PYTHON_MINOR+ and run it again."
    fi
    exit 1
  fi

  OMH_PYTHON="$OMH_SELECTED_PYTHON"
  OMH_RUNTIME_PYTHON="$OMH_PYTHON"
  OMH_SELECTED_PYTHON_VERSION="$(python_two_part_version "$OMH_PYTHON")"
}


say_header "$(msg installer_title)" "$(msg installer_subtitle)"
# The interpreter first, and before any network work. Resolving the release
# used to run ahead of it, so a machine with no usable Python and an
# unreachable GitHub reported the release failure -- burying the very
# diagnosis this step exists to surface, and losing the older
# "OMH_PYTHON was not found" one as well.
resolve_python
say_note "Python: $OMH_PYTHON ($OMH_SELECTED_PYTHON_VERSION)"
if [ -z "$OMH_PACKAGE_URL" ]; then
  case "$OMH_CHANNEL" in
    preview)
      OMH_PACKAGE_URL="$OMH_REPO_ARCHIVE_ROOT/heads/main.zip"
      if [ -z "$OMH_SOURCE_REF" ]; then
        OMH_SOURCE_REF="main"
      fi
      ;;
    stable)
      if [ -z "$OMH_VERSION" ]; then
        # GitHub answers /releases/latest with a 302 whose Location carries the
        # newest tag, so "latest" costs one header read and no API token. Only
        # the redirect target is fetched, never the release page.
        OMH_LATEST_LOCATION=""
        if command -v curl >/dev/null 2>&1; then
          OMH_LATEST_LOCATION="$(curl -sSI "$OMH_REPO_LATEST_URL" 2>/dev/null | tr -d '\r' | awk 'tolower($1) == "location:" { print $2 }' | tail -1)"
        elif command -v wget >/dev/null 2>&1; then
          OMH_LATEST_LOCATION="$(wget -qS --max-redirect=0 -O /dev/null "$OMH_REPO_LATEST_URL" 2>&1 | tr -d '\r' | awk 'tolower($1) == "location:" { print $2 }' | tail -1)"
        fi
        case "$OMH_LATEST_LOCATION" in
          */releases/tag/v*) OMH_VERSION="${OMH_LATEST_LOCATION##*/releases/tag/v}" ;;
          *)
            say "omh installer: could not resolve the latest release from $OMH_REPO_LATEST_URL."
            say "omh installer: set OMH_VERSION=1.0.6 to pin one, or OMH_CHANNEL=preview to track main."
            exit 1
            ;;
        esac
      fi
      case "$OMH_VERSION" in
        v*) OMH_TAG="$OMH_VERSION" ;;
        *) OMH_TAG="v$OMH_VERSION" ;;
      esac
      # The release workflow only accepts a vX.Y.Z tag and uploads a wheel
      # named from that version, so the asset URL is predictable. It is ~2.7 MB
      # against ~44 MB for the tag archive, which carries assets, tests, and
      # site that nothing needs to run omh. A version outside that shape has no
      # published asset to name, so it keeps the archive.
      OMH_RELEASE_VERSION="${OMH_TAG#v}"
      if printf '%s' "$OMH_RELEASE_VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
        OMH_PACKAGE_URL="$OMH_REPO_ASSET_ROOT/$OMH_TAG/oh_my_hermes-$OMH_RELEASE_VERSION-py3-none-any.whl"
      else
        OMH_PACKAGE_URL="$OMH_REPO_ARCHIVE_ROOT/tags/$OMH_TAG.zip"
      fi
      if [ -z "$OMH_SOURCE_REF" ]; then
        OMH_SOURCE_REF="$OMH_TAG"
      fi
      ;;
    local)
      say "omh installer: OMH_CHANNEL=local requires OMH_PACKAGE_URL to point at a local archive or path accepted by pip."
      exit 1
      ;;
    *)
      say "omh installer: unsupported OMH_CHANNEL '$OMH_CHANNEL' (expected preview, stable, or local)."
      exit 1
      ;;
  esac
elif [ -z "$OMH_SOURCE_REF" ]; then
  case "$OMH_CHANNEL" in
    local) OMH_SOURCE_REF="local" ;;
    stable)
      if [ -n "$OMH_VERSION" ]; then
        case "$OMH_VERSION" in
          v*) OMH_SOURCE_REF="$OMH_VERSION" ;;
          *) OMH_SOURCE_REF="v$OMH_VERSION" ;;
        esac
      else
        OMH_SOURCE_REF="custom-url"
      fi
      ;;
    preview) OMH_SOURCE_REF="main" ;;
    *) OMH_SOURCE_REF="custom-url" ;;
  esac
fi
say_note "$(msg channel): $OMH_CHANNEL"
say_note "Source ref: $OMH_SOURCE_REF"
say_note "$(msg mode): $OMH_INSTALL_MODE"
case "$OMH_INSTALL_MODE" in
  venv)
    install_into_venv
    ;;
  python)
    install_into_python
    ;;
  *)
    say "omh installer: unsupported OMH_INSTALL_MODE '$OMH_INSTALL_MODE' (expected venv or python)."
    exit 1
    ;;
esac

OMH_COMMAND_PATH="$(find_omh_command || true)"
if [ -n "$OMH_COMMAND_PATH" ]; then
  say_step "$(step_label "$OMH_EXPOSE_STEP")" "$(msg step_expose_command)"
  say_ok "$OMH_COMMAND_PATH"
  if ! command -v omh >/dev/null 2>&1; then
    OMH_COMMAND_DIR="$(dirname "$OMH_COMMAND_PATH")"
    say_note "'$OMH_COMMAND_DIR' is not on PATH for this shell."
    say_note "Add it with: export PATH=\"$OMH_COMMAND_DIR:\$PATH\""
    say_note "Until then, use: $OMH_COMMAND_PATH setup"
  fi
else
  say "omh installer: installed the package, but could not locate the omh command."
  say "Use '$OMH_RUNTIME_PYTHON -m omh.cli setup' as a fallback and check the selected Python scripts directory."
fi

if [ "$OMH_RUN_SETUP" = "1" ]; then
  set -- setup --channel "$OMH_CHANNEL" --package-url "$OMH_PACKAGE_URL" --source-ref "$OMH_SOURCE_REF" --command-package-updated

  if [ "$OMH_LANG_WAS_SET" = "1" ]; then
    set -- "$@" --language "$OMH_LANG"
  fi

  if [ "$OMH_CHANNEL" = "local" ] && [ -d "$OMH_PACKAGE_URL" ]; then
    set -- "$@" --source "$OMH_PACKAGE_URL"
  fi

  if [ "$OMH_AUTO_APPLY" = "0" ]; then
    set -- "$@" --skip-apply
  fi

  if [ -n "$OMH_VERSION" ]; then
    set -- "$@" --version "$OMH_VERSION"
  fi

  if [ "$OMH_WITH_PLUGIN" = "1" ]; then
    set -- "$@" --with-plugin
  fi

  if [ "$OMH_WITH_MCP" = "1" ]; then
    set -- "$@" --with-mcp
  fi

  if [ -n "$OMH_SCOPE" ]; then
    set -- "$@" --scope "$OMH_SCOPE"
  fi

  if [ -n "$OMH_PROFILE_PACKS" ]; then
    for OMH_PROFILE_PACK in $(printf '%s' "$OMH_PROFILE_PACKS" | tr ',' ' '); do
      set -- "$@" --profile-pack "$OMH_PROFILE_PACK"
    done
  fi

  if [ -n "$OMH_SETUP_PROFILES" ]; then
    for OMH_SETUP_PROFILE in $(printf '%s' "$OMH_SETUP_PROFILES" | tr ',' ' '); do
      set -- "$@" --profile "$OMH_SETUP_PROFILE"
    done
  fi

  if [ -n "$OMH_DEFAULT_EXECUTOR" ]; then
    set -- "$@" --default-executor "$OMH_DEFAULT_EXECUTOR"
  fi

  if [ -n "$OMH_SETUP_ARGS" ]; then
    # Intentional shell splitting: this is an advanced escape hatch for operators
    # who need to pass current omh setup flags before install.sh grows a stable
    # first-class environment variable for them.
    # shellcheck disable=SC2086
    set -- "$@" $OMH_SETUP_ARGS
  fi

  say_step "$(step_label "$OMH_SETUP_STEP")" "$(msg step_setup)"
  run_omh "$@"

  if [ "$OMH_RUN_DOCTOR" = "0" ]; then
    say_note "Skipped doctor check because OMH_RUN_DOCTOR=0."
  else
    say_step "$(step_label "$OMH_DOCTOR_STEP")" "$(msg step_doctor)"
    if [ -n "$OMH_SCOPE" ]; then
      run_omh --scope "$OMH_SCOPE" doctor
    else
      run_omh doctor
    fi
  fi
elif [ "$OMH_AUTO_APPLY" = "0" ] || [ "$OMH_WITH_PLUGIN" = "1" ] || [ "$OMH_WITH_MCP" = "1" ] || [ -n "$OMH_SCOPE" ] || [ -n "$OMH_PROFILE_PACKS" ] || [ -n "$OMH_SETUP_PROFILES" ] || [ -n "$OMH_DEFAULT_EXECUTOR" ] || [ -n "$OMH_SETUP_ARGS" ] || [ "$OMH_RUN_DOCTOR" = "0" ]; then
  say_note "Setup options were not applied because install.sh now installs the command only by default."
  say_note "Run 'omh setup' with those choices explicitly, or set OMH_RUN_SETUP=1 for advanced one-shot bootstrap."
fi

printf '\n'
say "$(color '1;36' "$(msg installed)")"
if command -v omh >/dev/null 2>&1; then
  say "$(msg next_path)"
elif [ -n "$OMH_COMMAND_PATH" ]; then
  say "$(msg next_command_path "$OMH_COMMAND_PATH")"
else
  say "Next: run '$OMH_PYTHON -m omh.cli setup' to connect OMH to Hermes, then '$OMH_PYTHON -m omh.cli doctor' to verify."
fi
