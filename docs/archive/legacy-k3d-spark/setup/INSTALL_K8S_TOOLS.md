# Kubernetes 工具安装命令

由于需要 sudo 权限，请手动执行以下命令：

## 快速安装（复制粘贴执行）

### 1. 安装 Docker

```bash
sudo apt update
sudo apt install -y docker.io
sudo systemctl enable docker
sudo systemctl start docker
sudo usermod -aG docker $USER
```

**重要**: 安装 Docker 后，需要重新登录或运行：
```bash
newgrp docker
```

### 2. 安装 kubectl

```bash
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl
rm kubectl
```

### 3. 安装 k3d

```bash
curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash
```

### 4. 验证安装

```bash
docker --version
kubectl version --client
k3d version
```

---

## 一键安装脚本（需要手动输入密码）

如果你想使用交互式脚本：

```bash
./scripts/install_k8s_tools.sh
```

脚本会在需要时提示输入 sudo 密码。

---

## 安装完成后

1. **重新登录**（使 Docker 组权限生效）
2. **创建 k3d 集群**:
   ```bash
   ./scripts/setup_k3d.sh
   ```
3. **部署 operator**:
   ```bash
   ./scripts/setup_operator.sh
   ```
