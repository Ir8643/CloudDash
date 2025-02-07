from flask import Flask, render_template, request, jsonify
import subprocess
import os
import json

app = Flask(__name__)

TERRAFORM_DIR = "terraform"
CREDENTIALS_FILE = "credentials.json"
CONFIG_FILE = "infrastructure_config.json"

def load_credentials():
    if os.path.exists(CREDENTIALS_FILE):
        with open(CREDENTIALS_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_credentials(credentials):
    with open(CREDENTIALS_FILE, 'w') as f:
        json.dump(credentials, f)

def load_infrastructure_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    return {
        'vpc_cidr': '10.0.0.0/16',
        'instance_type': 't2.micro',
        'instance_count': 1,
        'subnet_cidrs': ['10.0.1.0/24', '10.0.2.0/24'],
        'enable_public_ip': True,
        'volume_size': 20,
        'instance_name': 'terraform-instance'
    }

def save_infrastructure_config(config):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f)

@app.route('/')
def index():
    credentials = load_credentials()
    config = load_infrastructure_config()
    return render_template('index.html', credentials=credentials, config=config)

@app.route('/save_credentials', methods=['POST'])
def save_aws_credentials():
    credentials = {
        'aws_access_key': request.form.get('aws_access_key'),
        'aws_secret_key': request.form.get('aws_secret_key'),
        'aws_region': request.form.get('aws_region', 'us-west-2')
    }
    save_credentials(credentials)
    
    # Set environment variables for Terraform
    os.environ['AWS_ACCESS_KEY_ID'] = credentials['aws_access_key']
    os.environ['AWS_SECRET_ACCESS_KEY'] = credentials['aws_secret_key']
    os.environ['AWS_REGION'] = credentials['aws_region']
    
    return jsonify({'success': True})

@app.route('/save_config', methods=['POST'])
def save_config():
    config = {
        'vpc_cidr': request.form.get('vpc_cidr'),
        'instance_type': request.form.get('instance_type'),
        'instance_count': int(request.form.get('instance_count', 1)),
        'subnet_cidrs': request.form.getlist('subnet_cidrs[]'),
        'enable_public_ip': request.form.get('enable_public_ip') == 'true',
        'volume_size': int(request.form.get('volume_size', 20)),
        'instance_name': request.form.get('instance_name')
    }
    save_infrastructure_config(config)
    
    # Generate new Terraform configuration
    generate_terraform_config(config)
    
    return jsonify({'success': True})

def generate_terraform_config(config):
    subnet_count = len(config['subnet_cidrs'])
    
    terraform_config = f'''# Generated Terraform configuration
terraform {{
  required_providers {{
    aws = {{
      source  = "hashicorp/aws"
      version = "~> 4.0"
    }}
  }}
}}

provider "aws" {{
  region = "us-west-2"
}}

# Data source for availability zones
data "aws_availability_zones" "available" {{
  state = "available"
}}

# VPC
resource "aws_vpc" "main" {{
  cidr_block           = "{config['vpc_cidr']}"
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = {{
    Name = "main-vpc"
  }}
}}

# Internet Gateway
resource "aws_internet_gateway" "main" {{
  vpc_id = aws_vpc.main.id

  tags = {{
    Name = "main-igw"
  }}
}}

# Subnets
{generate_subnet_configs(config['subnet_cidrs'])}

# Route Table
resource "aws_route_table" "main" {{
  vpc_id = aws_vpc.main.id

  route {{
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }}

  tags = {{
    Name = "main-rt"
  }}
}}

{generate_route_table_associations(len(config['subnet_cidrs']))}

# Security Group
resource "aws_security_group" "instance_sg" {{
  name        = "instance-sg"
  description = "Security group for EC2 instances"
  vpc_id      = aws_vpc.main.id

  ingress {{
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }}

  egress {{
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }}
}}

locals {{
  subnet_ids = [
    {", ".join([f'aws_subnet.main_{i}.id' for i in range(subnet_count)])}
  ]
}}

# EC2 Instances
resource "aws_instance" "main" {{
  count         = {config['instance_count']}
  ami           = data.aws_ami.ubuntu.id
  instance_type = "{config['instance_type']}"
  subnet_id     = local.subnet_ids[count.index % {subnet_count}]

  vpc_security_group_ids = [aws_security_group.instance_sg.id]
  associate_public_ip_address = {str(config['enable_public_ip']).lower()}

  root_block_device {{
    volume_size = {config['volume_size']}
  }}

  tags = {{
    Name = "{config['instance_name']}-${{count.index + 1}}"
  }}
}}

# Latest Ubuntu AMI
data "aws_ami" "ubuntu" {{
  most_recent = true

  filter {{
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-focal-20.04-amd64-server-*"]
  }}

  filter {{
    name   = "virtualization-type"
    values = ["hvm"]
  }}

  owners = ["099720109477"] # Canonical
}}

# Output
output "instance_public_ips" {{
  value = aws_instance.main[*].public_ip
}}
'''
    
    with open(os.path.join(TERRAFORM_DIR, 'main.tf'), 'w') as f:
        f.write(terraform_config)

def generate_subnet_configs(subnet_cidrs):
    subnet_configs = []
    for i, cidr in enumerate(subnet_cidrs):
        subnet_configs.append(f'''
resource "aws_subnet" "main_{i}" {{
  vpc_id            = aws_vpc.main.id
  cidr_block        = "{cidr}"
  availability_zone = data.aws_availability_zones.available.names[{i}]

  tags = {{
    Name = "main-subnet-{i + 1}"
  }}
}}''')
    return '\n'.join(subnet_configs)

def generate_route_table_associations(subnet_count):
    associations = []
    for i in range(subnet_count):
        associations.append(f'''
resource "aws_route_table_association" "main_{i}" {{
  subnet_id      = aws_subnet.main_{i}.id
  route_table_id = aws_route_table.main.id
}}''')
    return '\n'.join(associations)

@app.route('/init', methods=['POST'])
def terraform_init():
    try:
        result = subprocess.run(['terraform', 'init'], 
                              cwd=TERRAFORM_DIR,
                              capture_output=True, 
                              text=True)
        return jsonify({
            'success': result.returncode == 0,
            'output': result.stdout,
            'error': result.stderr
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/plan', methods=['POST'])
def terraform_plan():
    try:
        result = subprocess.run(['terraform', 'plan'], 
                              cwd=TERRAFORM_DIR,
                              capture_output=True, 
                              text=True)
        return jsonify({
            'success': result.returncode == 0,
            'output': result.stdout,
            'error': result.stderr
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/apply', methods=['POST'])
def terraform_apply():
    try:
        result = subprocess.run(['terraform', 'apply', '-auto-approve'],
                              cwd=TERRAFORM_DIR,
                              capture_output=True,
                              text=True)
        return jsonify({
            'success': result.returncode == 0,
            'output': result.stdout,
            'error': result.stderr
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/destroy', methods=['POST'])
def terraform_destroy():
    try:
        result = subprocess.run(['terraform', 'destroy', '-auto-approve'],
                              cwd=TERRAFORM_DIR,
                              capture_output=True,
                              text=True)
        return jsonify({
            'success': result.returncode == 0,
            'output': result.stdout,
            'error': result.stderr
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

if __name__ == '__main__':
    app.run(debug=True)