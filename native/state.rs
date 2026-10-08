use crate::{config, detector::Limits};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    fs::{File, OpenOptions},
    io::Write,
    path::PathBuf,
};

pub fn session_hash(session: &str) -> String {
    format!("{:x}", Sha256::digest(session.as_bytes()))[..24].into()
}

#[derive(Debug, Serialize, Deserialize)]
pub struct State {
    pub session_id: String,
    #[serde(default, alias = "subagents_spawned")]
    pub subagents_reserved: u64,
    #[serde(default)]
    pub limits: Option<Limits>,
    #[serde(default)]
    pub overrides_blocked: u64,
}

impl State {
    fn empty(session: &str) -> Self {
        Self {
            session_id: session.into(),
            subagents_reserved: 0,
            limits: None,
            overrides_blocked: 0,
        }
    }
}

pub struct Session {
    id: String,
    path: PathBuf,
}

impl Session {
    pub fn new(id: &str) -> Self {
        let id = if id.is_empty() { "default" } else { id.trim() };
        Self {
            id: id.into(),
            path: config::directory("state")
                .join("sessions")
                .join(format!("{}.json", session_hash(id))),
        }
    }

    pub fn read(&self) -> State {
        let mut state = std::fs::read(&self.path)
            .ok()
            .and_then(|bytes| serde_json::from_slice::<State>(&bytes).ok())
            .unwrap_or_else(|| State::empty(&self.id));
        if let Some(limits) = &mut state.limits {
            limits.detected_phrase = None;
        }
        state
    }

    fn lock(&self) -> std::io::Result<File> {
        std::fs::create_dir_all(self.path.parent().expect("session directory"))?;
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(self.path.with_extension("lock"))?;
        lock.lock()?;
        Ok(lock)
    }

    pub fn transact<T>(&self, update: impl FnOnce(&mut State) -> (T, bool)) -> std::io::Result<T> {
        let _lock = self.lock()?;
        let mut state = self.read();
        let (result, changed) = update(&mut state);
        if changed {
            let mut temp =
                tempfile::NamedTempFile::new_in(self.path.parent().expect("session directory"))?;
            serde_json::to_writer(&mut temp, &state)?;
            temp.write_all(b"\n")?;
            temp.as_file().sync_all()?;
            temp.persist(&self.path).map_err(|error| error.error)?;
        }
        Ok(result)
    }

    pub fn reset(&self) -> std::io::Result<()> {
        let _lock = self.lock()?;
        match std::fs::remove_file(&self.path) {
            Err(error) if error.kind() != std::io::ErrorKind::NotFound => Err(error),
            _ => Ok(()),
        }
    }
}
