//! Core of the Vanta launcher. Contains no UI code.
//!
//! Vanta never redistributes Minecraft files: everything is downloaded from
//! official Mojang, Microsoft and mod loader sources at runtime.

pub mod accounts;
pub mod auth;
pub mod content;
pub mod download;
pub mod error;
pub mod http;
pub mod install;
pub mod instance;
pub mod java;
pub mod launch;
pub mod launcher;
pub mod loaders;
pub mod maven;
pub mod meta;
pub mod mrpack;
pub mod paths;
pub mod rules;
pub mod version;

pub use error::{Error, Result};
