"""File signature definitions for header/footer carving."""

from __future__ import annotations

from dataclasses import dataclass

_MB = 1024 * 1024
_GB = 1024 * _MB


@dataclass(frozen=True)
class FileSignature:
    """Magic-byte signature for file carving."""
    name: str           # Human-readable type name
    extension: str      # Default file extension
    header: bytes       # Magic bytes at file start
    footer: bytes | None  # Optional end marker (None = use max_size)
    max_size: int       # Maximum expected file size in bytes


# Ordered by frequency / usefulness for recovery
SIGNATURES: list[FileSignature] = [
    # ── Images ──
    FileSignature("JPEG", ".jpg", b"\xff\xd8\xff", b"\xff\xd9", 50 * _MB),
    FileSignature("PNG", ".png", b"\x89PNG\r\n\x1a\n", b"IEND\xaeB`\x82", 50 * _MB),
    FileSignature("GIF", ".gif", b"GIF89a", b"\x00\x3b", 30 * _MB),
    FileSignature("GIF87", ".gif", b"GIF87a", b"\x00\x3b", 30 * _MB),
    FileSignature("BMP", ".bmp", b"BM", None, 50 * _MB),
    FileSignature("WEBP", ".webp", b"RIFF", None, 50 * _MB),  # RIFF header; validated further
    FileSignature("TIFF-LE", ".tiff", b"II\x2a\x00", None, 100 * _MB),
    FileSignature("TIFF-BE", ".tiff", b"MM\x00\x2a", None, 100 * _MB),
    FileSignature("ICO", ".ico", b"\x00\x00\x01\x00", None, 5 * _MB),

    # ── Documents ──
    FileSignature("PDF", ".pdf", b"%PDF-", b"%%EOF", 500 * _MB),
    FileSignature("ZIP/DOCX/XLSX", ".zip", b"PK\x03\x04", None, 500 * _MB),
    FileSignature("RTF", ".rtf", b"{\\rtf", None, 100 * _MB),
    FileSignature("XML", ".xml", b"<?xml ", None, 50 * _MB),

    # ── Audio ──
    FileSignature("MP3-ID3", ".mp3", b"ID3", None, 50 * _MB),
    FileSignature("MP3-Sync", ".mp3", b"\xff\xfb", None, 50 * _MB),
    FileSignature("WAV", ".wav", b"RIFF", None, 500 * _MB),
    FileSignature("FLAC", ".flac", b"fLaC", None, 200 * _MB),
    FileSignature("OGG", ".ogg", b"OggS", None, 200 * _MB),
    FileSignature("M4A", ".m4a", b"\x00\x00\x00\x20ftypM4A", None, 200 * _MB),

    # ── Video ──
    FileSignature("MP4-isom", ".mp4", b"\x00\x00\x00\x18ftypisom", None, 4 * _GB),
    FileSignature("MP4-mp42", ".mp4", b"\x00\x00\x00\x1cftypisom", None, 4 * _GB),
    FileSignature("MP4-mp4", ".mp4", b"\x00\x00\x00\x18ftypmp4", None, 4 * _GB),
    FileSignature("AVI", ".avi", b"RIFF", None, 4 * _GB),
    FileSignature("MKV", ".mkv", b"\x1a\x45\xdf\xa3", None, 4 * _GB),
    FileSignature("FLV", ".flv", b"FLV\x01", None, 2 * _GB),
    FileSignature("WMV", ".wmv", b"\x30\x26\xb2\x75\x8e\x66\xcf\x11", None, 4 * _GB),
    FileSignature("MOV", ".mov", b"\x00\x00\x00\x14ftypqt", None, 4 * _GB),

    # ── Archives ──
    FileSignature("7ZIP", ".7z", b"7z\xbc\xaf\x27\x1c", None, 2 * _GB),
    FileSignature("RAR5", ".rar", b"Rar!\x1a\x07\x01\x00", None, 2 * _GB),
    FileSignature("RAR4", ".rar", b"Rar!\x1a\x07\x00", None, 2 * _GB),
    FileSignature("GZIP", ".gz", b"\x1f\x8b\x08", None, 500 * _MB),
    FileSignature("TAR", ".tar", b"ustar", None, 2 * _GB),  # offset 257 actually

    # ── Database / Data ──
    FileSignature("SQLite", ".db", b"SQLite format 3\x00", None, 500 * _MB),
    FileSignature("EXE/DLL", ".exe", b"MZ", None, 500 * _MB),
    FileSignature("PST", ".pst", b"!BDN", None, 2 * _GB),
    FileSignature("MBOX", ".mbox", b"From ", None, 500 * _MB),
    FileSignature("Parquet", ".parquet", b"PAR1", None, 500 * _MB),
    FileSignature("WASM", ".wasm", b"\x00asm", None, 50 * _MB),

    # ── Fonts ──
    FileSignature("WOFF", ".woff", b"wOFF", None, 10 * _MB),
    FileSignature("WOFF2", ".woff2", b"wOF2", None, 10 * _MB),
    FileSignature("OTF", ".otf", b"OTTO", None, 20 * _MB),
    FileSignature("TTF", ".ttf", b"\x00\x01\x00\x00", None, 20 * _MB),

    # ── Frontend / Web ──
    FileSignature("HTML-doctype", ".html", b"<!DOCTYPE html", None, 10 * _MB),
    FileSignature("HTML-doctype-lc", ".html", b"<!doctype html", None, 10 * _MB),
    FileSignature("HTML-tag", ".html", b"<html", None, 10 * _MB),
    FileSignature("SVG", ".svg", b"<svg ", None, 10 * _MB),
    FileSignature("SVG-xml", ".svg", b"<?xml", None, 10 * _MB),
    FileSignature("CSS-charset", ".css", b"@charset ", None, 5 * _MB),
    FileSignature("CSS-import", ".css", b"@import ", None, 5 * _MB),
    FileSignature("SCSS-import", ".scss", b"@import ", None, 5 * _MB),
    FileSignature("CSS-root", ".css", b":root {", None, 5 * _MB),
    FileSignature("CSS-root2", ".css", b":root{", None, 5 * _MB),
    FileSignature("SourceMap", ".map", b"{\"version\":3,", None, 50 * _MB),
    FileSignature("JSX-import", ".jsx", b"import React", None, 2 * _MB),
    FileSignature("TSX-import", ".tsx", b"import React", None, 2 * _MB),
    FileSignature("Vue-SFC", ".vue", b"<template>", None, 2 * _MB),
    FileSignature("Vue-SFC2", ".vue", b"<template ", None, 2 * _MB),
    FileSignature("Vue-script", ".vue", b"<script setup", None, 2 * _MB),
    FileSignature("Svelte", ".svelte", b"<script", None, 2 * _MB),

    # ── Backend / Server ──
    FileSignature("Shebang-node", ".js", b"#!/usr/bin/env node", None, 5 * _MB),
    FileSignature("Shebang-python", ".py", b"#!/usr/bin/env python", None, 5 * _MB),
    FileSignature("Shebang-python3", ".py", b"#!/usr/bin/python3", None, 5 * _MB),
    FileSignature("Shebang-bash", ".sh", b"#!/bin/bash", None, 2 * _MB),
    FileSignature("Shebang-sh", ".sh", b"#!/bin/sh", None, 2 * _MB),
    FileSignature("Shebang-ruby", ".rb", b"#!/usr/bin/env ruby", None, 5 * _MB),
    FileSignature("Shebang-perl", ".pl", b"#!/usr/bin/env perl", None, 5 * _MB),
    FileSignature("Shebang-php", ".php", b"#!/usr/bin/env php", None, 5 * _MB),
    FileSignature("PHP-open", ".php", b"<?php", None, 10 * _MB),
    FileSignature("PHP-short", ".php", b"<?=", None, 2 * _MB),
    FileSignature("Java-package", ".java", b"package ", None, 2 * _MB),
    FileSignature("Kotlin-package", ".kt", b"package ", None, 2 * _MB),
    FileSignature("Go-package", ".go", b"package ", None, 2 * _MB),
    FileSignature("Rust-use", ".rs", b"use std::", None, 2 * _MB),
    FileSignature("Rust-use2", ".rs", b"use crate::", None, 2 * _MB),
    FileSignature("CSharp-using", ".cs", b"using System", None, 5 * _MB),
    FileSignature("CSharp-ns", ".cs", b"namespace ", None, 5 * _MB),
    FileSignature("Swift-import", ".swift", b"import Foundation", None, 5 * _MB),
    FileSignature("Swift-import2", ".swift", b"import SwiftUI", None, 5 * _MB),
    FileSignature("Dart-import", ".dart", b"import 'package:", None, 5 * _MB),
    FileSignature("Dart-import2", ".dart", b"import 'dart:", None, 5 * _MB),
    FileSignature("Elixir-defmodule", ".ex", b"defmodule ", None, 2 * _MB),

    # ── Config / DevOps ──
    FileSignature("XML", ".xml", b"<?xml ", None, 50 * _MB),
    FileSignature("JSON-obj", ".json", b"{\"", None, 50 * _MB),
    FileSignature("JSON-arr", ".json", b"[{\"", None, 50 * _MB),
    FileSignature("YAML-doc", ".yaml", b"---\n", None, 10 * _MB),
    FileSignature("TOML-section", ".toml", b"[package]", None, 1 * _MB),
    FileSignature("TOML-section2", ".toml", b"[tool.", None, 1 * _MB),
    FileSignature("ENV", ".env", b"# .env", None, 256 * 1024),
    FileSignature("Dockerfile", ".dockerfile", b"FROM ", None, 256 * 1024),
    FileSignature("Docker-compose", ".yaml", b"services:", None, 256 * 1024),
    FileSignature("Nginx", ".conf", b"server {", None, 256 * 1024),
    FileSignature("Makefile", ".makefile", b".PHONY:", None, 256 * 1024),
    FileSignature("Gradle", ".gradle", b"plugins {", None, 256 * 1024),

    # ── Markdown / Documentation ──
    FileSignature("Markdown-h1", ".md", b"# ", None, 10 * _MB),
    FileSignature("Markdown-h2", ".md", b"## ", None, 10 * _MB),
    FileSignature("Markdown-h3", ".md", b"### ", None, 10 * _MB),
    FileSignature("Markdown-frontmatter", ".md", b"---\ntitle:", None, 10 * _MB),
    FileSignature("Markdown-frontmatter2", ".md", b"---\r\ntitle:", None, 10 * _MB),
    FileSignature("RST-heading", ".rst", b"====", None, 10 * _MB),
    FileSignature("LaTeX", ".tex", b"\\documentclass", None, 10 * _MB),
    FileSignature("LaTeX-begin", ".tex", b"\\begin{document}", None, 10 * _MB),
    FileSignature("AsciiDoc", ".adoc", b"= ", None, 10 * _MB),

    # ── Project / Package Files ──
    FileSignature("Solution", ".sln", b"\xef\xbb\xbf\r\nMicrosoft Visual Studio Solution File", None, 1 * _MB),
    FileSignature("Solution-noBOM", ".sln", b"\r\nMicrosoft Visual Studio Solution File", None, 1 * _MB),
    FileSignature("PackageJSON", ".json", b"{\"name\":", None, 1 * _MB),
    FileSignature("PackageJSON2", ".json", b"{\n  \"name\":", None, 1 * _MB),
    FileSignature("TSConfig", ".json", b"{\"compilerOptions\":", None, 256 * 1024),
    FileSignature("TSConfig2", ".json", b"{\n  \"compilerOptions\":", None, 256 * 1024),
    FileSignature("CargoToml", ".toml", b"[package]\nname", None, 256 * 1024),
    FileSignature("Gemfile", ".rb", b"source ", None, 256 * 1024),
    FileSignature("Pipfile", ".toml", b"[[source]]", None, 256 * 1024),

    # ── TypeScript / JavaScript ──
    FileSignature("TS-import", ".ts", b"import ", None, 5 * _MB),
    FileSignature("JS-use-strict", ".js", b"\"use strict\"", None, 5 * _MB),
    FileSignature("JS-use-strict2", ".js", b"'use strict'", None, 5 * _MB),
    FileSignature("ESM-export-default", ".js", b"export default", None, 5 * _MB),
    FileSignature("ESM-export-named", ".js", b"export {", None, 5 * _MB),
    FileSignature("ESM-export-const", ".js", b"export const", None, 5 * _MB),
    FileSignature("ESM-export-func", ".js", b"export function", None, 5 * _MB),
    FileSignature("ESM-export-class", ".js", b"export class", None, 5 * _MB),
    FileSignature("ESM-export-async", ".js", b"export async", None, 5 * _MB),

    # ── Python ──
    FileSignature("Python-import", ".py", b"import ", None, 5 * _MB),
    FileSignature("Python-from", ".py", b"from ", None, 5 * _MB),
    FileSignature("Python-class", ".py", b"class ", None, 5 * _MB),
    FileSignature("Python-def", ".py", b"def ", None, 5 * _MB),
    FileSignature("Python-docstring", ".py", b'\"\"\"', None, 5 * _MB),
    FileSignature("Jupyter", ".ipynb", b"{\"cells\":", None, 50 * _MB),
    FileSignature("Jupyter2", ".ipynb", b"{\n \"cells\":", None, 50 * _MB),

    # ── SQL ──
    FileSignature("SQL-create", ".sql", b"CREATE TABLE", None, 50 * _MB),
    FileSignature("SQL-create-lc", ".sql", b"create table", None, 50 * _MB),
    FileSignature("SQL-insert", ".sql", b"INSERT INTO", None, 50 * _MB),
    FileSignature("SQL-select", ".sql", b"SELECT ", None, 50 * _MB),
    FileSignature("SQL-dump", ".sql", b"-- MySQL dump", None, 500 * _MB),
    FileSignature("SQL-pgdump", ".sql", b"-- PostgreSQL database dump", None, 500 * _MB),

    # ── C / C++ ──
    FileSignature("C-include", ".c", b"#include <", None, 5 * _MB),
    FileSignature("C-include2", ".c", b"#include \"", None, 5 * _MB),
    FileSignature("H-pragma", ".h", b"#pragma once", None, 2 * _MB),
    FileSignature("H-ifndef", ".h", b"#ifndef ", None, 2 * _MB),

    # ── Certificates / Keys ──
    FileSignature("PEM-cert", ".pem", b"-----BEGIN CERTIFICATE-----", None, 64 * 1024),
    FileSignature("PEM-pubkey", ".pem", b"-----BEGIN PUBLIC KEY-----", None, 64 * 1024),
    FileSignature("SSH-pubkey", ".pub", b"ssh-rsa ", None, 16 * 1024),
    FileSignature("SSH-ed25519", ".pub", b"ssh-ed25519 ", None, 16 * 1024),

    # ── Misc Text ──
    FileSignature("RTF", ".rtf", b"{\\rtf", None, 100 * _MB),
    FileSignature("BOM-UTF8", ".txt", b"\xef\xbb\xbf", None, 10 * _MB),
    FileSignature("CSV-header", ".csv", b"id,", None, 100 * _MB),
    FileSignature("CSV-header2", ".csv", b"\"id\",", None, 100 * _MB),
    FileSignature("Log-timestamp", ".log", b"[2", None, 500 * _MB),
]

# Pre-compute the longest header length for scanning window
MAX_HEADER_LEN = max(len(s.header) for s in SIGNATURES)
