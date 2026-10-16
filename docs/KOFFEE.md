# Koffee Binding Affinity

The optional Koffee stage runs after docking, on completed docked poses only. It is a downstream assessment: its kcal/mol prediction is shown separately and never changes the docking, SA, ADMET, HYDE, or ranking decision.

Set these values outside the repository. For Docker Compose, place the Kvantify license in a
host directory readable by the app container's uid (10001), then configure the host directory
and filename in the Compose env file:

```dotenv
KOFFEE_DOCKER_BIN=docker
KOFFEE_IMAGE=koffee-binding-affinity:latest
KOFFEE_LICENSE_HOST_DIR=/secure/kvantify
KOFFEE_LICENSE_FILENAME=license.txt
KOFFEE_TIMEOUT_SECONDS=7200
KOFFEE_OPTIMIZATION_LEVEL=1
KOFFEE_SYSTEM_PRE_OPTIMIZATION=true
KOFFEE_LIGAND_STRAIN_IN_SCORE=false
KOFFEE_PROTEIN_PREPARATION_MODE=remove_non_protein
```

Compose mounts that host directory read-only at `/etc/kvantify` and sets
`KOFFEE_LICENSE_FILE=/etc/kvantify/<filename>` inside the app container. The license directory and
file must be traversable/readable by uid 10001. For a non-containerized app process, set
`KOFFEE_LICENSE_FILE` directly to the full path of the vendor license file. After changing the
Compose environment, recreate the app container.

The adapter mounts each private batch directory at `/host` and the license directory read-only at `/license`; it calls Koffee with `--default-directory`, `--input-archive`, `--license-file`, `--log-directory`, `--cpus`, `--optimization-level`, system-pre-optimization, ligand-strain, and protein-preparation flags verified against Koffee Binding Affinity v0.9.4.

Koffee input requirements are enforced before submission: a prepared PDB with explicit hydrogens and a single-molecule, 3D, explicitly hydrogenated SDF pose per candidate. The integration does not re-dock, embed, protonate, or otherwise fabricate poses. Non-standard protein residues, cofactors, or protonation states needing Koffee's `--additional-residue-kinds` remain a scientific preparation task and are rejected rather than guessed.

`results.tsv` is parsed by its generated ligand file path and an explicit manifest, never by result order. More-negative scores mean stronger predicted binding only within an appropriate comparable set; they are model predictions, not experimental binding affinities. A batch can retain successful results if Koffee omits an individual ligand row; omitted candidates are recorded with their own failure reason and can be retried through the existing batch controls.
