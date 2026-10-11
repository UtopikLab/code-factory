# Bare-Metal Kubernetes Storage & K3s Setup Guide

This document records the complete, step-by-step setup procedure for provisioning tiered host storage and dynamic Kubernetes storage classes on a Dell bare-metal server.

---

## 1. Hardware & Target Layout Overview

| Device Node | Physical Hardware | Size / Usable | Role & Filesystem | Mount Point |
| :--- | :--- | :--- | :--- | :--- |
| `/dev/sdb` | Dell BOSS RAID1 | ~240 GB | Host OS, Containerd, Logs (`ext4`) | `/`, `/boot`, `/boot/efi` |
| `/dev/sda` | 3x SSD RAID5 | ~1.9 TB | Persistent Cluster Storage (`xfs`) | `/mnt/k8s-data-ssd` |
| `/dev/nvme0n1` | NVMe | ~1.9 TB | Kubelet & Fast Local PVs (`ext4`) | `/var/lib/kubelet`, `/mnt/k8s-local-nvme` |
| `/dev/sdc` | Dell Dual SD | ~32 GB | *Unassigned / Disabled* | None |

---

## 2. Host Storage Allocation & LVM Configuration

### 2.1 Maximize Root Logical Volume on Dell BOSS
The Ubuntu automated installer initially allocates a partial volume (~100 GB). Expand it to utilize the full disk space:

```bash
# Extend the root logical volume to use 100% of remaining free space
sudo lvextend -l +100%FREE /dev/mapper/ubuntu--vg-ubuntu--lv

# Resize filesystem online
sudo resize2fs /dev/mapper/ubuntu--vg-ubuntu--lv
```

### 2.2 Configure the NVMe Drive (`/dev/nvme0n1`)
Partition the high-IOPS NVMe drive for both Kubelet internals and fast local persistent volumes:

```bash
# Clear any existing partition signatures
sudo wipefs -a /dev/nvme0n1

# Initialize LVM PV and VG
sudo pvcreate /dev/nvme0n1
sudo vgcreate vg_nvme_local /dev/nvme0n1

# Create 500GB volume for Kubelet emptyDir / ephemeral storage
sudo lvcreate -L 500G -n lv_kubelet vg_nvme_local
sudo mkfs.ext4 -L k8s_kubelet /dev/vg_nvme_local/lv_kubelet

# Create remaining space volume (~1.3TB) for fast local PVs
sudo lvcreate -L 1.3T -n lv_local_fast vg_nvme_local
sudo mkfs.ext4 -L k8s_local_fast /dev/vg_nvme_local/lv_local_fast
```

### 2.3 Configure the SSD RAID5 Array (`/dev/sda`)
Format the hardware-redundant array for general-purpose persistent volumes:

```bash
# Clear any existing signatures
sudo wipefs -a /dev/sda

# Initialize LVM PV and VG
sudo pvcreate /dev/sda
sudo vgcreate vg_data_ssd /dev/sda

# Allocate 100% of free space to standard storage
sudo lvcreate -l 100%FREE -n lv_storage_standard vg_data_ssd
sudo mkfs.xfs -L k8s_ssd_pool /dev/vg_data_ssd/lv_storage_standard
```

### 2.4 Mount Directories & Persist in `/etc/fstab`

```bash
# Create mount points
sudo mkdir -p /var/lib/kubelet
sudo mkdir -p /mnt/k8s-local-nvme
sudo mkdir -p /mnt/k8s-data-ssd

# Append mounts to fstab
sudo tee -a /etc/fstab <<'EOF'
/dev/vg_nvme_local/lv_kubelet        /var/lib/kubelet        ext4    defaults,noatime    0 2
/dev/vg_nvme_local/lv_local_fast     /mnt/k8s-local-nvme     ext4    defaults,noatime    0 2
/dev/vg_data_ssd/lv_storage_standard  /mnt/k8s-data-ssd       xfs     defaults,noatime    0 2
EOF

# Mount all targets and verify
sudo mount -a
lsblk
```

---

## 3. Kubernetes (K3s) Installation

Install lightweight single-node Kubernetes (K3s) with standard permissions for `kubectl`:

```bash
# Install K3s server
curl -sfL https://k3s.io | sh -s - server --write-kubeconfig-mode 644

# Export kubeconfig to shell environment
echo 'export KUBECONFIG=/etc/rancher/k3s/k3s.yaml' >> ~/.bashrc
source ~/.bashrc

# Verify node is Ready
kubectl get nodes -o wide
```

---

## 4. StorageClass Provisioner Configuration

Configure the built-in Rancher Local Path Provisioner to map and recognize both host paths.

### 4.1 Update Provisioner ConfigMap
Apply the path mappings to `local-path-config`:

```bash
kubectl apply -f - <<'EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: local-path-config
  namespace: kube-system
data:
  config.json: |-
    {
      "nodePathMap": [
        {
          "node": "DEFAULT_PATH_FOR_NON_LISTED_NODES",
          "paths": [
            "/mnt/k8s-data-ssd",
            "/mnt/k8s-local-nvme"
          ]
        }
      ]
    }
  helperPod.yaml: |-
    apiVersion: v1
    kind: Pod
    metadata:
      name: helper-pod
    spec:
      priorityClassName: system-node-critical
      tolerations:
        - key: CriticalAddonsOnly
          operator: Exists
      containers:
      - name: helper-pod
        image: rancher/mirrored-library-busybox:1.36.1
        imagePullPolicy: IfNotPresent
EOF
```

Restart the provisioner pod to reload settings:

```bash
kubectl rollout restart deployment local-path-provisioner -n kube-system
```

### 4.2 Remove Default Annotation from Built-in Class

```bash
kubectl patch storageclass local-path -p '{"metadata": {"annotations":{"storageclass.kubernetes.io/is-default-class":"false"}}}'
```

### 4.3 Create Hardware-Pinned StorageClasses

```bash
cat <<EOF | kubectl apply -f -
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: k8s-sc-ssd-replicated
  annotations:
    storageclass.kubernetes.io/is-default-class: "true"
provisioner: rancher.io/local-path
volumeBindingMode: WaitForFirstConsumer
reclaimPolicy: Retain
parameters:
  nodePath: /mnt/k8s-data-ssd
---
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: k8s-sc-nvme-fast
provisioner: rancher.io/local-path
volumeBindingMode: WaitForFirstConsumer
reclaimPolicy: Delete
parameters:
  nodePath: /mnt/k8s-local-nvme
EOF
```

Verify the storage class list:

```bash
kubectl get sc
```

Expected output:
```text
NAME                              PROVISIONER             RECLAIMPOLICY   VOLUMEBINDINGMODE      ALLOWVOLUMEEXPANSION   AGE
k8s-sc-nvme-fast                  rancher.io/local-path   Delete          WaitForFirstConsumer   false                  ...
k8s-sc-ssd-replicated (default)   rancher.io/local-path   Retain          WaitForFirstConsumer   false                  ...
local-path                        rancher.io/local-path   Delete          WaitForFirstConsumer   false                  ...
```

---

## 5. Storage Verification & Test Workload

### 5.1 Deploy Test PVCs and Verification Pod

```bash
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: test-pvc-ssd
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: k8s-sc-ssd-replicated
  resources:
    requests:
      storage: 1Gi
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: test-pvc-nvme
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: k8s-sc-nvme-fast
  resources:
    requests:
      storage: 1Gi
---
apiVersion: v1
kind: Pod
metadata:
  name: storage-verifier
spec:
  containers:
    - name: writer
      image: busybox:latest
      command: ["/bin/sh", "-c"]
      args:
        - >
          echo "Verified: SSD RAID5 persistent storage" > /mnt/ssd/raid5-test.txt &&
          echo "Verified: NVMe fast persistent storage" > /mnt/nvme/nvme-test.txt &&
          sleep 3600
      volumeMounts:
        - name: vol-ssd
          mountPath: /mnt/ssd
        - name: vol-nvme
          mountPath: /mnt/nvme
  volumes:
    - name: vol-ssd
      persistentVolumeClaim:
        claimName: test-pvc-ssd
    - name: vol-nvme
      persistentVolumeClaim:
        claimName: test-pvc-nvme
EOF
```

### 5.2 Validate Volume Host Paths

Confirm mapping from Kubernetes:

```bash
kubectl get pv -o custom-columns=NAME:.metadata.name,CLAIM:.spec.claimRef.name,PATH:.spec.hostPath.path
```

Verify files from inside the container:

```bash
kubectl exec storage-verifier -- cat /mnt/ssd/raid5-test.txt
kubectl exec storage-verifier -- cat /mnt/nvme/nvme-test.txt
```

Verify files directly on the host system:

```bash
sudo find /mnt/k8s-data-ssd /mnt/k8s-local-nvme -name "*.txt" -exec head -v -n 10 {} +
```

### 5.3 Cleanup Verification Resources

```bash
# Delete test workloads
kubectl delete pod storage-verifier
kubectl delete pvc test-pvc-ssd test-pvc-nvme

# Remove retained test directories from SSD array
sudo rm -rf /mnt/k8s-data-ssd/pvc-*
```
