# Add-ons: candidates for the catalog (research, 2026-09-26)

Research. Taken into `catalog.yaml` on 2026-09-26: Amiigo, NXThemes Installer,
ThemezerNX, Checkpoint, Goldleaf, sphaira, SimpleModManager, SimpleModDownloader,
EdiZon Overlay, ovl-sysmodules, SaltyNX-Tool, NX Activity Log,
NX-Update-Checker, NXMP, NooDS, Turnips (see docs/ADDONS.md for what waits). Every release below
was fetched via `gh api`, downloaded and unpacked; NACP names read with
`switchagent.nro.nro_info_from_bytes`. Firmware 23.x compatibility was
**not** verified for any of them (only MissionControl states "up to 22.5.0").
Sysmodules with `boot2.flag` need a reboot after install/update.

## Popularity sources

- hb-appstore `https://switch.cdn.fortheusers.org/repo.json` -- 501 packages,
  `app_dls` = downloads. Column "dl" below.
- NH Switch Guide https://switch.hacks.guide/homebrew/ -- JKSV, Goldleaf, ftpd,
  NXThemes Installer, NX-Shell, EdiZon, SimpleModManager, SysDVR, sys-clk,
  ldn_mitm, Tesla, MissionControl, sys-con, sys-botbase.
- GBAtemp (via curl; WebFetch gets 403):
  https://gbatemp.net/threads/my-2026-setup-guide-with-links-and-capabilities.679308/ (emulation: RetroArch nightly, NooDS, PPSSPP),
  https://gbatemp.net/threads/must-have-homebrew.639484/ (Checkpoint, JKSV, pplay).
  Reply/view counts not extracted reliably.
- Reddit: NOT reachable (login wall). 4PDA: only a model summary of
  https://4pda.to/forum/index.php?showtopic=900987&st=40 -- unverified.
- kefir 919 ships: sys-patch, MissionControl, sys-con, ovlSysmodules,
  EdiZon overlay (proferabg), NX-Activity-Log, NXThemes Installer, linkalho,
  sphaira config, plus DBI/TorrentShopNX/pipensx/sys-clk-OC/NX-FanControl.

## Candidates, by priority

"user cfg" = file a person edits -> needs `keep:`.

| # | Name | Repo | ★ | Release | Asset | SD paths | Type | Needs | kefir | dl | Risks / notes | Why |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | NXThemes Installer | exelix11/SwitchThemeInjector | 1240 | nxt-3.0.2, 2026-09-22 | `NXThemesInstaller.nro` | `switch/NXThemesInstaller/`, NACP "NXThemes Installer" | app | — | yes | 775902 | theme applies after reboot; NH Guide default | home-menu themes |
| 2 | Goldleaf | XorTroll/Goldleaf | 3041 | 1.2.1, 2026-09-22 | `Goldleaf.nro` | `switch/Goldleaf/`, "Goldleaf" | app | — | no | 650083 | also installs NSP; NH Guide default | file/content manager |
| 3 | MissionControl | ndeadly/MissionControl | 3540 | v0.15.2, 2026-06-18 | `MissionControl-*.zip` | `atmosphere/contents/010000000000bd00/`, `atmosphere/exefs_patches/{bluetooth,btm,hid}_patches/`, `config/MissionControl/missioncontrol.ini.template` | sysmodule+patches | — | yes | 617569 | FW up to 22.5.0 stated; reboot | BT controllers (PS/Xbox) |
| 4 | ThemezerNX | suchmememanyskill/themezer-nx | 147 | 3.3.0, 2026-09-23 | `themezer-nx.nro` | `switch/…`, "Themezer-NX" | app | NXThemes Installer | no | 440714 | network | theme catalog on console |
| 5 | Checkpoint | BernardoGiordano/Checkpoint | 3088 | v5.2.0, 2026-08-26 | `Checkpoint.nro` | `switch/Checkpoint/`, "Checkpoint"; saves in `switch/Checkpoint/saves/` (never touch) | app | — | no | 306888 | overlaps JKSV | save manager |
| 6 | EdiZon Overlay | proferabg/EdiZon-Overlay | 401 | v1.0.15, 2026-04-24 | `ovlEdiZon.ovl` | `switch/.overlays/ovlEdiZon.ovl`, "EdiZon" | overlay | Ultrahand | yes | 184205 | cheats themselves separate | cheats in-game |
| 7 | Amiigo | CompSciOrBust/Amiigo | 231 | 2.4.1, 2026-01-18 | `Amiigo.nro` | `switch/Amiigo/`, "Amiigo" | app | emuiibo | no | 128005 | no license in API | make amiibo for emuiibo |
| 8 | SysDVR | exelix11/SysDVR | 2027 | v6.3, 2026-02-10 | `SysDVR.zip` | `atmosphere/contents/00FF0000A53BB665/`, `config/sysdvr/rtsp` (user cfg: mode flag), `switch/SysDVR-conf.nro` "SysDVR Settings" | sysmodule+app | PC client | no | 126910 | memory use; reboot | stream console to PC |
| 9 | NXMP | proconsule/nxmp | 322 | v0.9.3, 2025-12-17 | `nxmp-*.zip` | zip root `nxmp/` (`nxmp.nro`, user cfg `config.ini`, `eqpresets.ini`) | app | — | no | 88362 | layout is not SD layout -- target dir unverified | video player |
| 10 | SimpleModDownloader | PoloNX/SimpleModDownloader | 166 | 2.3.0, 2025-12-07 | `SimpleModDownloader.nro` | `switch/…` | app | — | no | 77256 | writes mods itself | GameBanana mods |
| 11 | sys-con | o0Zz/sys-con (live fork) | 213 | 2.0.0, 2026-09-25 | `sys-con-*.zip` | `atmosphere/contents/690000000000000D/`, `config/sys-con/config.ini` (user cfg), `switch/sys-con.nro` "sys-con" | sysmodule+app | — | yes | 76383 | reboot | USB controllers |
| 12 | SimpleModManager | nadrino/SimpleModManager | 468 | 2.1.4, 2026-02-23 | `SimpleModManager.nro` | `switch/…` | app | — | no | 75986 | moves mod files itself; NH Guide | toggle mods |
| 13 | Switchfin | dragonflylee/switchfin | 875 | 0.9.4, 2026-08-25 | `Switchfin.nro` (65 MB) | `switch/Switchfin/` | app | Jellyfin server | no | 59815 | big | Jellyfin client |
| 14 | SysDVR Overlay | Hartie95/sysdvr-overlay | 36 | v1.0.15, 2026-02-13 | `SysDVR-Overlay-*.zip` | `switch/.overlays/sysdvr-overlay.ovl` | overlay | SysDVR, Ultrahand | no | 59266 | — | SysDVR control in-game |
| 15 | sphaira | NaGaa95/sphaira (redirect from ITotalJustice) | 828 | 1.0.7, 2026-09-09 | `sphaira.zip` | `switch/sphaira/sphaira.nro` | app | — | cfg only | 52886 | can replace hbmenu -- app only | hb menu, file manager, store |
| 16 | NX-Update-Checker | 16BitWonder/NX-Update-Checker | 141 | v1.5.4.2, 2026-01-23 | `NX-Update-Checker.nro` | `switch/…` | app | — | no | 52041 | network | game update check |
| 17 | Turnips | averne/Turnips | 162 | v1.7.7, 2026-05-16 | `Turnips-*.zip` (wrapper `out/`) | `switch/…` | app | — | no | 41959 | ACNH only | turnip prices |
| 18 | ldn_mitm | spacemeowx2/ldn_mitm | 943 | v1.25.1, 2026-04-08 | `ldn_mitm_v*.zip` | `atmosphere/contents/4200000000000010/`, `switch/.overlays/ldnmitm_config.ovl` "ldn_mitm", `switch/ldnmitm_config/` "ldnmitm cfg" | sysmodule+ovl+app | Ultrahand | no | 40461 | hb-appstore ships fork DefenderOfHyrule 1.21.2; reboot; NH Guide | LAN play |
| 19 | NooDS | Hydr8gon/NooDS | 1158 | tag `release`, 2026-08-09 | `noods-switch.zip` | bare `noods.nro` (NACP v0.1) | app | — | no | 40205 | tag never changes -> no version | NDS emulator |
| 20 | SaltyNX-Tool | masagrator/SaltyNX-Tool | 53 | 1.1.1, 2025-11-15 | `SaltyNX-Tool.nro` | `switch/…` | app | SaltyNX | no | 33699 | — | SaltyNX settings |
| 21 | ovl-sysmodules | ppkantorski/ovl-sysmodules | 94 | v1.5.3, 2026-06-25 | `ovlSysmodules.ovl` | `switch/.overlays/`, "Sysmodules" | overlay | Ultrahand | yes | — | — | toggle sysmodules (replaces WerWolv's) |
| 22 | NX Activity Log | zdm65477730/NX-Activity-Log | 258 | v1.5.9, 2026-08-24 | `NX-Activity-Log.nro` | `switch/NX-Activity-Log/` | app | — | yes | — | — | play-time stats |
| 23 | ftpsrv | ITotalJustice/ftpsrv | 60 | 1.2.2, 2025-01-16 | `switch_application.zip` / `switch_sysmod.zip` | `switch/ftpsrv.nro` or `atmosphere/contents/420000000000011B/`; `config/ftpsrv/config.ini.template` | app or sysmodule | — | no | — | **no published SHA-256 digest** | FTP (replaces ftpd) |
| 24 | PNGShot | J-D-K/PNGShot | 80 | 2.6.0, 2026-03-06 | `PNGShot.zip` | `atmosphere/contents/010000000000C236/`, `atmosphere/exefs_patches/vi_patches/` | sysmodule+patches | — | no | — | FW-bound patches; reboot | PNG screenshots |
| 25 | NSParentalControl | TristanIsrael/NSParentalControl | 30 | v1.3.0, 2026-01-03 | `NSParentalControl.zip` | `atmosphere/contents/4200000000003103/`, `switch/.overlays/parental_control.ovl` | sysmodule+overlay | Ultrahand | no | — | wrapper dir + `__MACOSX` junk; reboot | kids' time limits |

Owner's decision: **sys-patch** (impeeza/sys-patch, ★820, v1.6.2.3,
2026-06-17, `atmosphere/contents/420000000000000B/` + `sys-patch-overlay.ovl`,
in kefir) -- sigpatches, dual-use.

## Rejected

- RetroArch (947k dl) -- buildbot, not GitHub releases. PPSSPP (257k) -- ppsspp.org.
- Hekate, Payload_launcher, TegraExplorer -- boot chain.
- Horizon-OC (live sys-clk replacement) -- writes `atmosphere/exosphere.bin`, `atmosphere/kips/hoc.kip`.
- Memory-Kit -- swaps mesosphere. uLaunch (63k) -- replaces qlaunch `0100000000001000`, too risky.
- Abandoned: Tesla-menu, WerWolv nx-ovlloader/ovl-sysmodules, sys-clk (2024), hb-appstore app (2023), NX-Shell (2022), EdiZon-SE (2023), sys-tune (stable 2023), ftpd (2024), sys-ftpd, pplay, switch-cheats-updater, AmiiboGenerator, cathery/sys-con, tallbl0nde/NX-Activity-Log.
- nxdumptool -- GitHub stable 2022; fresh builds only in hb-appstore.
- mGBA -- `.7z` asset, 2025-03. melonDS -- fork with ★16.
- Tinfoil, TorrentShopNX, pipensx -- piracy stores.
- Breeze -- ships its own hbl.nsp/hbmenu + 3 sysmodules.
- Niche: wiliwili, TsVitch, Tetris/UltraGB overlays, fastCFWswitch (reboots to payload), linkalho (impeeza fork, clean layout, narrow use).
