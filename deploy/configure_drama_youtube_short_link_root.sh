#!/usr/bin/env bash
set -euo pipefail

mode="${1:-}"
if [[ "$mode" != "--check" && "$mode" != "--apply" ]]; then
  echo "usage: $0 --check|--apply" >&2
  exit 2
fi

mount_root=/mnt/data-disk
expected_uuid=3e8ac4e8-7770-456d-9e89-2ec5dd405fa8
base=/mnt/data-disk/drama-youtube-short-links
s2l="$base/s2l"
root="$s2l/youtube"
app_user=drama-youtube
web_user=nginx

[[ "$(id -u)" == "0" ]] || { echo "root is required" >&2; exit 1; }
[[ "$(findmnt -n -o UUID -T "$mount_root")" == "$expected_uuid" ]] || {
  echo "data-disk UUID mismatch" >&2
  exit 1
}
getent passwd "$app_user" >/dev/null
getent passwd "$web_user" >/dev/null
command -v setfacl >/dev/null
command -v getfacl >/dev/null

if [[ "$mode" == "--apply" ]]; then
  install -d -o "$app_user" -g "$app_user" -m 0750 "$base" "$s2l" "$root"
  for path in "$base" "$s2l" "$root"; do
    setfacl -b "$path"
    setfacl -m u::rwx,u:"$web_user":r-x,g::r-x,m::r-x,o::--- "$path"
  done
  setfacl -k "$root"
  setfacl -d -m u::rwx,u:"$web_user":r--,g::---,m::r--,o::--- "$root"
fi

for path in "$base" "$s2l" "$root"; do
  [[ "$(stat -c '%U:%G %a' "$path")" == "$app_user:$app_user 750" ]] || {
    echo "owner/mode mismatch: $path" >&2
    exit 1
  }
  access_acl="$(getfacl -cp "$path")"
  [[ "$access_acl" == *$'user:nginx:r-x'* && "$access_acl" == *$'mask::r-x'* && "$access_acl" == *$'other::---'* ]] || {
    echo "nginx access ACL mismatch: $path" >&2
    exit 1
  }
done

root_acl="$(getfacl -cp "$root")"
[[ "$root_acl" == *$'default:user::rwx'* \
  && "$root_acl" == *$'default:user:nginx:r--'* \
  && "$root_acl" == *$'default:group::---'* \
  && "$root_acl" == *$'default:mask::r--'* \
  && "$root_acl" == *$'default:other::---'* ]] || {
  echo "default ACL mismatch: $root" >&2
  exit 1
}
runuser -u "$app_user" -- test -w "$root"
runuser -u "$web_user" -- test -r "$root"
runuser -u "$web_user" -- test -x "$root"
echo "drama YouTube short-link root: PASS"
