# encoding: utf-8
"""
protocol.py

Created by Thomas Mangin on 2009-08-25.
Copyright (c) 2009-2013 Exa Networks. All rights reserved.
"""

from exabgp.rib.table import Table
from exabgp.rib.delta import Delta

from exabgp.reactor.network.outgoing import Outgoing
from exabgp.reactor.network.error import SizeError

from exabgp.bgp.message import Message
from exabgp.bgp.message.nop import NOP
from exabgp.bgp.message.open import Open
from exabgp.bgp.message.open.routerid import RouterID
from exabgp.bgp.message.open.capability import Capabilities
from exabgp.bgp.message.open.capability.negotiated import Negotiated
from exabgp.bgp.message.update import Update
from exabgp.bgp.message.update.eor import EOR
from exabgp.bgp.message.keepalive import KeepAlive
from exabgp.bgp.message.notification import Notification, Notify
from exabgp.bgp.message.refresh import RouteRefresh

from exabgp.reactor.api.processes import ProcessError

from exabgp.logger import Logger,FakeLogger,LazyFormat

# This is the number of chuncked message we are willing to buffer, not the number of routes
MAX_BACKLOG = 15000

_NOP = NOP()

class Protocol (object):
	decode = True

	def __init__ (self,peer,connection=None):
		try:
			self.logger = Logger()
		except RuntimeError:
			self.logger = FakeLogger()
		self.peer = peer
		self.neighbor = peer.neighbor
		self.connection = connection
		self.negotiated = Negotiated()

		self.delta = Delta(Table(peer))

		# XXX: FIXME: check the the -19 is correct (but it is harmless)
		# The message size is the whole BGP message _without_ headers
		self.message_size = Message.MAX_LEN-Message.HEADER_LEN

	# XXX: we use self.peer.neighbor.peer_address when we could use self.neighbor.peer_address

	def me (self,message):
		return "Peer %15s ASN %-7s %s" % (self.peer.neighbor.peer_address,self.peer.neighbor.peer_as,message)

	def connect (self):
		# allows to test the protocol code using modified StringIO with a extra 'pending' function
		if not self.connection:
			peer = self.neighbor.peer_address
			local = self.neighbor.local_address
			md5 = self.neighbor.md5
			ttl = self.neighbor.ttl
			self.connection = Outgoing(peer.afi,peer.ip,local.ip,md5,ttl)

			if self.peer.neighbor.api.neighbor_changes:
				self.peer.reactor.processes.connected(self.peer.neighbor.peer_address)

	def close (self,reason='unspecified'):
		if self.connection:
			# must be first otherwise we could have a loop caused by the raise in the below
			self.connection.close()
			self.connection = None

			try:
				if self.peer.neighbor.api.neighbor_changes:
					self.peer.reactor.processes.down(self.peer.neighbor.peer_address,reason)
			except ProcessError:
				self.logger.message(self.me('could not send notification of neighbor close to API'))


	def write (self,message):
		if self.neighbor.api.send_packets:
			self.peer.reactor.processes.send(self.peer.neighbor.peer_address,message[18],message[:19],message[19:])
		for boolean in self.connection.writer(message):
			yield boolean

	# Read from network .......................................................

	def read_message (self,keepalive_comment=''):
		try:
			for length,msg,header,body in self.connection.reader():
				if not length:
					yield _NOP
		except ValueError,e:
			code,subcode,string = str(e).split(' ',2)
			raise Notify(int(code),int(subcode),string)

		if self.neighbor.api.receive_packets:
			self.peer.reactor.processes.receive(self.peer.neighbor.peer_address,msg,header,body)

		if msg == Message.Type.UPDATE:
			self.logger.message(self.me('<< UPDATE'))

			if length == 30 and body.startswith(EOR.PREFIX):
				yield EOR().factory(body)

			if self.neighbor.api.receive_routes:
				update = Update().factory(self.negotiated,body)

				for route in update.routes:
					self.logger.routes(LazyFormat(self.me(''),str,route))

				self.peer.reactor.processes.routes(self.neighbor.peer_address,update.routes)
				yield update
			else:
				yield _NOP

		elif msg == Message.Type.KEEPALIVE:
			self.logger.message(self.me('<< KEEPALIVE%s' % keepalive_comment))
			yield KeepAlive()

		elif msg == Message.Type.NOTIFICATION:
			self.logger.message(self.me('<< NOTIFICATION'))
			yield Notification().factory(body)

		elif msg == Message.Type.ROUTE_REFRESH:
			self.logger.message(self.me('<< ROUTE-REFRESH'))
			yield RouteRefresh().factory(body)

		elif msg == Message.Type.OPEN:
			yield Open().factory(body)

		else:
			self.logger.message(self.me('<< NOP (unknow type %d)' % msg))
			yield NOP().factory(msg)


	def read_open (self,_open,ip):
		for message in self.read_message():
			if message.TYPE == NOP.TYPE:
				yield message
			else:
				break

		if message.TYPE != Open.TYPE:
			raise Notify(5,1,'The first packet recevied is not an open message (%s)' % message)

		self.negotiated.received(message)

		if not self.negotiated.asn4:
			if self.neighbor.local_as.asn4():
				raise Notify(2,0,'peer does not speak ASN4, we are stuck')
			else:
				# we will use RFC 4893 to convey new ASN to the peer
				self.negotiated.asn4

		if self.negotiated.peer_as != self.neighbor.peer_as:
			raise Notify(2,2,'ASN in OPEN (%d) did not match ASN expected (%d)' % (message.asn,self.neighbor.peer_as))

		# RFC 6286 : http://tools.ietf.org/html/rfc6286
		#if message.router_id == RouterID('0.0.0.0'):
		#	message.router_id = RouterID(ip)
		if message.router_id == RouterID('0.0.0.0'):
			raise Notify(2,3,'0.0.0.0 is an invalid router_id according to RFC6286')

		if message.router_id == self.neighbor.router_id and message.asn == self.neighbor.local_as:
			raise Notify(2,3,'BGP Indendifier collision (%s) on IBGP according to RFC 6286' % message.router_id)

		if message.hold_time and message.hold_time < 3:
			raise Notify(2,6,'Hold Time is invalid (%d)' % message.hold_time)

		if self.negotiated.multisession not in (True,False):
			# XXX: FIXME: should we not use a string and perform a split like we do elswhere ?
			# XXX: FIXME: or should we use this trick in the other case ?
			raise Notify(*self.negotiated.multisession)

		self.logger.message(self.me('<< %s' % message))
		yield message

	def read_keepalive (self,comment=''):
		for message in self.read_message(comment):
			if message.TYPE == NOP.TYPE:
				yield message
			else:
				break

		if message.TYPE != KeepAlive.TYPE:
			raise Notify(5,2)

		yield message

	#
	# Sending message to peer
	#

	def new_open (self,restarted):
		sent_open = Open().new(
			4,
			self.neighbor.local_as,
			self.neighbor.router_id.ip,
			Capabilities().new(self.neighbor,restarted),
			self.neighbor.hold_time
		)

		self.negotiated.sent(sent_open)

		# we do not buffer open message in purpose
		for _ in self.write(sent_open.message()):
			yield _NOP

		self.logger.message(self.me('>> %s' % sent_open))
		yield sent_open

	def new_keepalive (self,comment=''):
		keepalive = KeepAlive()

		for _ in self.write(keepalive.message()):
			yield _NOP

		self.logger.message(self.me('>> KEEPALIVE%s' % comment))
		yield keepalive

	def new_notification (self,notification):
		for _ in self.write(notification.message()):
			yield _NOP
		self.logger.error(self.me('>> NOTIFICATION (%d,%d,"%s")' % (notification.code,notification.subcode,notification.data)))
		yield notification

	# XXX: FIXME: for half of the functions we return numbers for the other half Message object.
	# XXX: FIXME: consider picking one or the other
	def new_update (self):
		# XXX: This should really be calculated once only
		for number in self._announce('UPDATE',self.peer.bgp.delta.updates(self.negotiated,self.neighbor.group_updates)):
			yield number

	# XXX: FIXME: for half of the functions we return numbers for the other half Message object.
	# XXX: FIXME: consider picking one or the other
	def new_eors (self):
		eor = EOR().new(self.negotiated.families)
		for number in self._announce(str(eor),eor.updates(self.negotiated)):
			yield number

	def _announce (self,name,generator):
		def chunked (generator,size):
			chunk = ''
			number = 0
			for data in generator:
				if len(data) > size:
					raise SizeError('Can not send BGP update larger than %d bytes on this connection.' % size)
				if len(chunk) + len(data) <= size:
					chunk += data
					number += 1
					continue
				yield number,chunk
				chunk = data
				number = 1
			if chunk:
				yield number,chunk

		for number,update in chunked(generator,self.message_size):
			for boolean in self.write(update):
				if boolean:
					self.logger.message(self.me('>> %d %s(s)' % (number,name)))
					yield number
				else:
					yield 0