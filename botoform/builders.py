import traceback

from botoform.enriched import EnrichedVPC

from botoform.util import (
  BotoConnections,
  Log,
  update_tags,
  make_tag_dict,
  get_port_range,
  get_ids,
  collection_len,
)

from botoform.subnetallocator import allocate

from uuid import uuid4

class EnvironmentBuilder(object):

    def __init__(self, vpc_name, config=None, region_name=None, profile_name=None, log=None):
        """
        vpc_name:
         The human readable Name tag of this VPC.

        config:
         The dict returned by botoform.config.ConfigLoader's load method.
        """
        self.vpc_name = vpc_name
        self.config = config if config is not None else {}
        self.log = log if log is not None else Log()
        self.boto = BotoConnections(region_name, profile_name)
        self.reflect = False

    def apply_all(self):
        """Build the environment specified in the config."""
        try:
            self._apply_all(self.config)
        except Exception as e:
            self.log.emit('Botoform failed to build environment!', 'error')
            self.log.emit('Failure reason: {}'.format(e), 'error')
            self.log.emit(traceback.format_exc(), 'debug')
            self.log.emit('Tearing down failed environment!', 'error')
            #self.evpc.terminate()
            raise

    def _apply_all(self, config):

        # Make sure amis is setup early. (TODO: raise exception if missing)
        self.amis = config['amis']

        # set a var for no_cfg.
        no_cfg = {}

        # attach EnrichedVPC to self.
        self.evpc = EnrichedVPC(self.vpc_name, self.boto.region_name, self.boto.profile_name)

        # the order of these method calls matters for new VPCs.
        self.route_tables(config.get('route_tables', no_cfg))
        self.subnets(config.get('subnets', no_cfg))
        self.associate_route_tables_with_subnets(config.get('subnets', no_cfg))
        self.endpoints(config.get('endpoints', []))
        self.key_pairs(config.get('key_pairs', []))
        self.security_groups(config.get('security_groups', no_cfg))
        self.instance_roles(config.get('instance_roles', no_cfg))
        self.security_group_rules(config.get('security_groups', no_cfg))

        for instance in self.evpc.instances:
            self.log.emit('waiting for {} to start'.format(instance.identity))
            instance.wait_until_running()

        try:
            self.log.emit('locking instances to prevent termination')
            self.evpc.lock_instances()
        except:
            self.log.emit('could not lock instances, continuing...', 'warning')
        self.log.emit('done! don\'t you look awesome. : )')

    def build_vpc(self, cidrblock):
        """Build VPC"""
        self.log.emit('creating vpc ({}, {})'.format(self.vpc_name, cidrblock))
        vpc = self.boto.ec2.create_vpc(CidrBlock = cidrblock)

        self.log.emit('tagging vpc (Name:{})'.format(self.vpc_name), 'debug')
        update_tags(vpc, Name = self.vpc_name)

        self.log.emit('modifying vpc for dns support', 'debug')
        vpc.modify_attribute(EnableDnsSupport={'Value': True})
        self.log.emit('modifying vpc for dns hostnames', 'debug')
        vpc.modify_attribute(EnableDnsHostnames={'Value': True})

        igw_name = 'igw-' + self.vpc_name
        self.log.emit('creating internet_gateway ({})'.format(igw_name))
        gw = self.boto.ec2.create_internet_gateway()
        self.log.emit('tagging gateway (Name:{})'.format(igw_name), 'debug')
        update_tags(gw, Name = igw_name)

        self.log.emit('attaching igw to vpc ({})'.format(igw_name))
        vpc.attach_internet_gateway(
            DryRun=False,
            InternetGatewayId=gw.id,
            VpcId=vpc.id,
        )

    def route_tables(self, route_cfg):
        """Build route_tables defined in config"""
        for rt_name, data in route_cfg.items():
            longname = '{}-{}'.format(self.evpc.name, rt_name)
            route_table = self.evpc.get_route_table(longname)
            if route_table is None:
                self.log.emit('creating route_table ({})'.format(longname))
                if data.get('main', False) == True:
                    route_table = self.evpc.get_main_route_table()
                else:
                    route_table = self.evpc.create_route_table()
                self.log.emit('tagging route_table (Name:{})'.format(longname), 'debug')
                update_tags(route_table, Name = longname)

            # TODO: move to separate method ...
            # gatewayId, natGatewayId, networkInterfaceId, vpcPeeringConnectionId or instanceId
            # add routes to route_table defined in configuration.
            for route in data.get('routes', []):
                destination, target = route
                self.log.emit('adding route {} to route_table ({})'.format(route, longname))
                if target.lower() == 'internet_gateway':
                    # this is ugly but we assume only one internet gateway.
                    route_table.create_route(
                        DestinationCidrBlock = destination,
                        GatewayId = list(self.evpc.internet_gateways.all())[0].id,
                    )

    def subnets(self, subnet_cfg):
        """Build subnets defined in config."""
        sizes = sorted([x['size'] for x in subnet_cfg.values()])
        cidrs = allocate(self.evpc.cidr_block, sizes)

        azones = self.evpc.azones

        subnets = {}
        for size, cidr in zip(sizes, cidrs):
            subnets.setdefault(size, []).append(cidr)

        for name, sn in subnet_cfg.items():
            longname = '{}-{}'.format(self.evpc.name, name)
            az_letter = sn.get('availability_zone', None)
            if az_letter is not None:
                az_name = self.evpc.region_name + az_letter
            else:
                az_index = int(name.split('-')[-1]) - 1
                az_name = azones[az_index]

            cidr = subnets[sn['size']].pop()
            self.log.emit('creating subnet {} in {}'.format(cidr, az_name))
            subnet = self.evpc.create_subnet(
                          CidrBlock = str(cidr),
                          AvailabilityZone = az_name
            )
            self.log.emit('tagging subnet (Name:{})'.format(longname), 'debug')
            update_tags(
                subnet,
                Name = longname,
                description = sn.get('description', ''),
            )
            # Modifying the subnet's public IP addressing behavior.
            if sn.get('public', False) == True:
                subnet.map_public_ip_on_launch = True

    def associate_route_tables_with_subnets(self, subnet_cfg):
        for sn_name, sn_data in subnet_cfg.items():
            rt_name = sn_data.get('route_table', None)
            if rt_name is None:
                continue
            self.log.emit('associating rt {} with sn {}'.format(rt_name, sn_name))
            self.evpc.associate_route_table_with_subnet(rt_name, sn_name)

    def endpoints(self, route_tables):
        """Build VPC endpoints for given route_tables"""
        if len(route_tables) == 0:
            return None
        self.log.emit(
            'creating vpc endpoints in {}'.format(', '.join(route_tables))
        )
        self.evpc.vpc_endpoint.create_all(route_tables)

    def security_groups(self, security_group_cfg):
        """Build Security Groups defined in config."""

        for sg_name, rules in security_group_cfg.items():
            sg = self.evpc.get_security_group(sg_name)
            if sg is not None:
                continue
            longname = '{}-{}'.format(self.evpc.name, sg_name)
            self.log.emit('creating security_group {}'.format(longname))
            security_group = self.evpc.create_security_group(
                GroupName   = longname,
                Description = longname,
            )
            self.log.emit(
                'tagging security_group (Name:{})'.format(longname), 'debug'
            )
            update_tags(security_group, Name = longname)

    def security_group_rules(self, security_group_cfg):
        """Build Security Group Rules defined in config."""
        msg = "'{}' into '{}' over ports {} ({})"
        for sg_name, rules in security_group_cfg.items():
            sg = self.evpc.get_security_group(sg_name)
            permissions = []
            for rule in rules:
                protocol = rule[1]
                from_port, to_port = get_port_range(rule[2], protocol)
                src_sg = self.evpc.get_security_group(rule[0])

                permission = {
                    'IpProtocol' : protocol,
                    'FromPort'   : from_port,
                    'ToPort'     : to_port,
                }

                if src_sg is None:
                    permission['IpRanges'] = [{'CidrIp' : rule[0]}]
                else:
                    permission['UserIdGroupPairs'] = [{'GroupId':src_sg.id}]

                permissions.append(permission)

                fmsg = msg.format(rule[0],sg_name,rule[2],rule[1].upper())
                self.log.emit(fmsg)

            sg.authorize_ingress(
                IpPermissions = permissions
            )

    def key_pairs(self, key_pair_cfg):
        key_pair_cfg.append('default')
        for short_key_pair_name in key_pair_cfg:
            if self.evpc.key_pair.get_key_pair(short_key_pair_name) is None:
                self.log.emit('creating key pair {}'.format(short_key_pair_name))
                self.evpc.key_pair.create_key_pair(short_key_pair_name)

    def instance_roles(self, instance_role_cfg):
        roles = {}
        roles_with_eips = []
        for role_name, role_data in instance_role_cfg.items():
            desired_count = role_data.get('count', 0)
            if role_data.get('eip', False) == True:
                roles_with_eips.append(role_name)
            role_instances = self.instance_role(
                                 role_name,
                                 role_data,
                                 desired_count,
                             )
            roles[role_name] = role_instances

        # tag instances and volumes.
        for role_name, role_instances in roles.items():
            self.tag_instances(role_name, role_instances)

        # TODO: move to own method / function.
        # deal with EIP roles.
        for role_name in roles_with_eips:
            role_instances = roles[role_name]
            eip1_msg = 'allocating eip and associating with {}'
            eip2_msg = 'allocated eip {} and associated with {}'
            for instance in role_instances:
                self.log.emit(eip1_msg.format(instance.identity))
                eip = instance.allocate_eip()
                self.log.emit(eip2_msg.format(eip.public_ip, instance.identity))

    def instance_role(self, role_name, role_data, desired_count):
        self.log.emit('creating role: {}'.format(role_name))
        ami = self.amis[role_data['ami']][self.evpc.region_name]

        key_pair = self.evpc.key_pair.get_key_pair(
                       role_data.get('key_pair', 'default')
                   )

        security_groups = map(
            self.evpc.get_security_group,
            role_data.get('security_groups', [])
        )

        subnets = map(
            self.evpc.get_subnet,
            role_data.get('subnets', [])
        )

        if len(subnets) == 0:
            self.log.emit(
                'no subnets found for role: {}'.format(role_name), 'warning'
            )
            # exit early.
            return None

        # sort by subnets by amount of instances, smallest first.
        subnets = sorted(
                      subnets,
                      key = lambda sn : collection_len(sn.instances),
                  )

        # determine the count of this role's existing instances.
        # Note: we look for role in all subnets, not just the listed subnets.
        existing_count = len(self.evpc.get_role(role_name))

        if existing_count >= desired_count:
            # for now we exit early, maybe terminate extras...
            self.log.emit(existing_count + ' ' + desired_count, 'debug')
            return None

        # determine count of additional instances needed to reach desired_count.
        needed_count      = desired_count - existing_count
        needed_per_subnet = needed_count / len(subnets)
        needed_remainder  = needed_count % len(subnets)

        role_instances = []

        for subnet in subnets:
            # ensure Run_Instance_Idempotency.html#client-tokens
            client_token = str(uuid4())

            # figure out how many instances this subnet needs to create ...
            existing_in_subnet = len(self.evpc.get_role(role_name, subnet.instances.all()))
            count = needed_per_subnet - existing_in_subnet
            if needed_remainder != 0:
                needed_remainder -= 1
                count += 1

            if count == 0:
                # skip this subnet, it doesn't need to launch any instances.
                continue

            subnet_name = make_tag_dict(subnet)['Name']
            msg = '{} instances of role {} launching into {}'
            self.log.emit(msg.format(count, role_name, subnet_name))

            # create a batch of instances in subnet!
            instances = subnet.create_instances(
                       ImageId           = ami,
                       InstanceType      = role_data.get('instance_type'),
                       MinCount          = count,
                       MaxCount          = count,
                       KeyName           = key_pair.name,
                       SecurityGroupIds  = get_ids(security_groups),
                       ClientToken       = client_token,
            )
            # accumulate all new instances into a single list.
            role_instances += instances

        # cast role Instance objets to EnrichedInstance objects.
        return self.evpc.get_instances(role_instances)

    def tag_instances(self, role_name, instances):
        """
        Accept a list of EnrichedInstances, create name and role tags.

        Also tag volumes.
        """
        msg1 = 'tagging instance {} (Name:{}, role:{})'
        msg2 = 'tagging volumes for instance {} (Name:{})'
        for instance in instances:
            instance_id = instance.id.lstrip('i-')
            hostname = self.evpc.name + '-' + role_name + '-' + instance_id
            self.log.emit(msg1.format(instance.identity, hostname, role_name))
            update_tags(instance, Name = hostname, role = role_name)

            for volume in instance.volumes.all():
                self.log.emit(msg2.format(instance.identity, hostname))
                update_tags(volume, Name = hostname)


