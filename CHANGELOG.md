# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-08-13

### Added

- Initial public release.
- **Transports**: Modbus TCP, UDP, RTU, ASCII, and TLS (with optional client
  certificate support and `--no-verify`).
- **Commands**: `read`, `write`, `scan`, and `watch` covering holding/input
  registers, coils, and discrete inputs.
- **Datatype engine**: `u16`, `i16`, `u32`, `i32`, `u64`, `i64`, `f32`, `f64`,
  and `str` register/value conversions with configurable byte order and word
  order plus a `--scale` factor.
- **Output modes**: human-readable tables and value lines, or machine-readable
  JSON (`--json`) with clean stdout (all diagnostics on stderr).
- **Watch command**: change-detection diffing between polls, configurable
  interval/iterations, and optional file logging via `--output`.
- **Scan command**: range scanning with non-zero filtering and `--all` mode.
- **Protocol limits**: FC1/FC2 (2000 bits), FC3/FC4 (125 registers),
  FC6 (1 register), FC15 (1968 coils), FC16 (123 registers) are enforced
  up front with clear usage errors.
- **Robustness**: connection retries with backoff, clear exit codes
  (0-5), and verbose/traceback diagnostics via `-v`.
