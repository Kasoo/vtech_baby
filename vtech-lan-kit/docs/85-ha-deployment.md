# 85 — Deploying the native layer in a Home Assistant integration

This describes how an integration packages and prepares the native layer (the SDK libraries and the
companion) on a Home Assistant host. `90-ha-runtime.md` covers what happens after the libraries are
loaded.

## Constraints

- **No compiler on the host.** Home Assistant installations (HA OS, Container, Supervised) run the
  integration's Python inside HA's own container: musl-based on the official images, glibc on some Core
  installs. There is no `gcc` and no `podman`, so nothing native can be built at install time.
- **Only the SDK libraries are user-supplied** (`00-prerequisites.md`): the integration must not ship,
  download or redistribute those binaries — including *patched* ones, which are still derived from the
  vendor binaries — though it may point the user at a public source to obtain them (`../sdk/README.md`).
  The **SDK license key, the provisioning constants and the per-camera credentials are not user-typed**:
  the license key and provisioning constants are embedded app-wide static values (small numeric facts,
  shareable under interoperability exceptions), and the credentials are derived from the owner's pairing
  QR (`05-provisioning.md`).
- **Hosts:** 64-bit x86_64 or aarch64. On aarch64, a 4K- or 16K-page kernel.

This yields one model: **ship the tooling and a prebuilt companion for each architecture, and patch the
user's SDK libraries on the host at setup time.** Patching is pure Python (`tooling/patch_sdk.py`), so it
runs inside HA.

## What ships, and what the user supplies

| Artifact | Where it comes from | In the integration package? |
|---|---|---|
| `elfpatch.py`, `patch_sdk.py`, `companion/renames.txt` | this kit's `tooling/` | **Yes** |
| `libtk.so` for **x86_64** and for **aarch64** | built from this kit's companion source (below) | **Yes**, prebuilt, one per architecture |
| `vtech_provision.py` + app-wide QR/HKDF constants | this kit's `client/` (`05-provisioning.md`) | **Yes** |
| The four SDK libraries, for the host's architecture | the user (from a public source; not shipped) | **No** |
| SDK license key | embedded app-wide constant (`client/`) | **Yes** |
| Pairing QR (yields per-camera UID + AV password; authKey derived) | the user (scan/photo) | **No** |
| Patched SDK library set | generated on the host by `patch_sdk()` | **No**: produced at setup |

`renames.txt` and the companion are a matched pair: the patch renames imports to exactly the tokens the
companion exports. Always ship them from the same kit revision.

## Building the companions to ship

```sh
ARCH=x86_64  sh tooling/companion/build.sh dist/x86_64
ARCH=aarch64 sh tooling/companion/build.sh dist/aarch64
```

- Each build runs in the `manylinux2014` image for that architecture, so its glibc floor is 2.17.
- One binary per architecture loads on **both glibc and musl** hosts.
- Building for a foreign architecture needs `podman` plus `qemu-user` binfmt.
- `build.sh` prints the companion's `DT_NEEDED` entries, its glibc version floor and its exported
  tokens. Check that the floor stays at 2.17.

Package the results at a per-architecture path, e.g. `native/x86_64/libtk.so` and
`native/aarch64/libtk.so`.

## Setup flow on the host

Run all of this off the event loop (in an executor): it does file I/O and hashes several MB.

1. **Collect the SDK libraries** from the user. The usable form is an upload of the app download itself
   (`../sdk/README.md` names the app and version); `../tooling/extract_sdk.py` takes the four libraries
   for the host's architecture out of it, checks their architecture and SDK version, and replaces the
   SDK directory only once they pass:
   ```python
   from .tooling.extract_sdk import extract_sdk, ExtractError   # NotAPackage, WrongArch, MissingLibs, WrongVersion
   info = extract_sdk(uploaded_file, "<config>/<domain>/sdk", arch)
   ```
   Delete the upload afterwards. If the SDK directory already holds a usable set, skip the upload.
2. **Check the host.** Map `platform.machine()` to `x86_64` or `aarch64`, and refuse anything else
   (32-bit armv7 / i386). The SDK directory's architecture (`patch_sdk.sdk_arch()`) must match the
   host's.
3. **Patch into a per-architecture cache directory**, for example
   `<config>/<domain>/lib/<arch>/`:
   ```python
   from .tooling.patch_sdk import patch_sdk, PatchError
   info = patch_sdk(user_sdk_dir, out_dir, companion=os.path.join(PKG, "native", arch, "libtk.so"))
   ```
   - The output directory must be on a filesystem that allows executable mappings (not mounted
     `noexec`). The HA config directory is fine on standard installs.
   - `patch_sdk()` verifies its own output before writing it, and raises `PatchError` on anything
     unexpected. Show that error to the user as a setup failure.
   - Only SDK version `patch_sdk.SDK_VERSION` (4.3.8.1) is accepted: any other raises `PatchError`
     (`../sdk/README.md`). `info["version"]` reports it.
   - The step is idempotent. `PATCHED.json` records the input, companion and tooling hashes, and a
     repeat call with unchanged inputs returns immediately with `info["cached"] is True`.
4. **Check before loading.** The reference client's `preflight(libdir)` confirms that the host
   architecture matches the library set and that the kernel page size is within the libraries' segment
   alignment. It raises a readable error instead of an opaque `dlopen` failure.
5. **Load** the companion, then `libTUTKGlobalAPIs`, `libNebulaAPIs`, `libIOTCAPIs` and `libAVAPIs`,
   using `ctypes.CDLL(path, mode=os.RTLD_NOW | os.RTLD_LOCAL)` with absolute paths. No environment
   variables are needed: the patched libraries carry `DT_RUNPATH $ORIGIN`, so their dependencies resolve
   from the same directory on both glibc and musl. Do this **once per process** (`90-ha-runtime.md`).

The license key does not have to live in a file. The reference client embeds it (`DEFAULT_LICENSE_KEY`)
and uses that unless a `LICENSE_KEY.txt` overrides it; an integration can equally keep its own copy and
pass it directly to `TUTK_SDK_Set_License_Key`.

## Updates

- When the SDK, the companion or the tooling changes (a new integration release, or the user replacing
  the SDK files), the manifest no longer matches and `patch_sdk()` re-patches on the next setup.
- A process that has **already loaded** the libraries keeps using them, because they are never unloaded
  (`90-ha-runtime.md`). After a re-patch, ask for a Home Assistant restart (e.g. a repair issue) rather
  than loading a second copy.

## Unsupported hosts

Fail the configuration flow with the specific reason when:
- the host is 32-bit (armv7, i386);
- the host is not x86_64 or aarch64;
- the kernel page size exceeds the SDK libraries' segment alignment (64K-page aarch64 kernels);
- the SDK architecture doesn't match the host.

`preflight()` and `patch_sdk()` already produce these errors.
