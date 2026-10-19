#!/usr/bin/env bash
# Install shell completions for vwcli + vw (bash, zsh, fish; Linux + macOS).
#
# Uses the register-python-argcomplete binary shipped inside the vwcli uv
# tool venv, so the generated scripts always match the installed argcomplete.
set -euo pipefail

TOOL_DIR="$(uv tool dir 2>/dev/null || true)"
REGISTER="${TOOL_DIR:+$TOOL_DIR/vwcli/bin/register-python-argcomplete}"
if [[ -z "$REGISTER" || ! -x "$REGISTER" ]]; then
  echo "register-python-argcomplete not found; run 'uv tool install .' first." >&2
  exit 1
fi

gen() { "$REGISTER" --shell "$1" "$2"; }

append_once() {
  # append a line to an rc file if it is not already there
  local line="$1" file="$2"
  grep -qxF "$line" "$file" 2>/dev/null && return 0
  printf '%s\n' "$line" >> "$file"
  echo "Added to $file: $line"
}

install_bash() {
  local dir="${XDG_DATA_HOME:-$HOME/.local/share}/bash-completion/completions"
  local autoload=0

  if [[ "${OSTYPE:-}" == darwin* ]] && command -v brew >/dev/null 2>&1 && [[ -d "$(brew --prefix)/etc/bash_completion.d" ]]; then
    # Homebrew bash-completion: files here are sourced automatically.
    dir="$(brew --prefix)/etc/bash_completion.d"
    autoload=1
  elif [[ -d /usr/share/bash-completion || -d /etc/bash_completion.d ]]; then
    # bash-completion package: user dir is picked up by the lazy loader.
    autoload=1
  fi

  mkdir -p "$dir"
  gen bash vwcli > "$dir/vwcli"
  gen bash vw > "$dir/vw"
  echo "bash completions installed to $dir"

  if [[ "$autoload" -eq 0 ]]; then
    # No bash-completion infrastructure found - source the files directly.
    local rc="$HOME/.bashrc"
    if [[ "${OSTYPE:-}" == darwin* && ! -f "$rc" ]]; then
      rc="$HOME/.bash_profile"  # macOS login shells read .bash_profile
    fi
    append_once "[[ -r \"$dir/vwcli\" ]] && source \"$dir/vwcli\"" "$rc"
    append_once "[[ -r \"$dir/vw\" ]] && source \"$dir/vw\"" "$rc"
  fi
}

install_zsh() {
  # Only useful when zsh is present or the user already has a zshrc.
  if ! command -v zsh >/dev/null 2>&1 && [[ ! -f "${ZDOTDIR:-$HOME}/.zshrc" ]]; then
    return 0
  fi
  local dir="${XDG_DATA_HOME:-$HOME/.local/share}/zsh/site-functions"
  mkdir -p "$dir"
  gen zsh vwcli > "$dir/_vwcli"
  gen zsh vw > "$dir/_vw"
  echo "zsh completions installed to $dir"

  local zshrc="${ZDOTDIR:-$HOME}/.zshrc"
  touch "$zshrc"
  append_once 'fpath=($HOME/.local/share/zsh/site-functions $fpath)' "$zshrc"
  append_once 'autoload -Uz compinit && compinit' "$zshrc"
}

install_fish() {
  # Skip for users without any fish presence.
  if ! command -v fish >/dev/null 2>&1 && [[ ! -d "${XDG_CONFIG_HOME:-$HOME/.config}/fish" ]]; then
    return 0
  fi
  local dir="${XDG_CONFIG_HOME:-$HOME/.config}/fish/completions"
  mkdir -p "$dir"
  gen fish vwcli > "$dir/vwcli.fish"
  gen fish vw > "$dir/vw.fish"
  echo "fish completions installed to $dir"
}

install_bash
install_zsh
install_fish
