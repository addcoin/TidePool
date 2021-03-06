import weakref
import binascii
import util
import StringIO
import lib.settings as settings

from twisted.internet import defer
from lib.exceptions import SubmitException

import lib.logger
log = lib.logger.get_logger('template_registry')

from mining.interfaces import Interfaces
from extranonce_counter import ExtranonceCounter

class JobIdGenerator(object):
	'''Generate pseudo-unique job_id. It does not need to be absolutely unique,
	because pool sends "clean_jobs" flag to clients and they should drop all previous jobs.'''
	counter = 0

	@classmethod
	def get_new_id(cls):
		cls.counter += 1
		if cls.counter % 0xffff == 0:
			cls.counter = 1
		return "%x" % cls.counter

class TemplateRegistry(object):
	'''Implements the main logic of the pool. Keep track
	on valid block templates, provide internal interface for stratum
	service and implements block validation and submits.'''

	def __init__(self, template_generator, bitcoin_rpc, instance_id, on_template_callback, on_block_callback):
		log.debug("Got to Template Registry")

		self.prevhashes = {}
		self.jobs = weakref.WeakValueDictionary()

		self.template_generator = template_generator

		self.extranonce_counter = ExtranonceCounter(instance_id)
		self.extranonce2_size = template_generator.get_extranonce_size() - self.extranonce_counter.get_size()

		self.bitcoin_rpc = bitcoin_rpc
		self.on_block_callback = on_block_callback
		self.on_template_callback = on_template_callback

		self.last_template = None
		self.update_in_progress = False
		self.GBT_RPC_ATTEMPT = None
		self.last_block_update_start_time = None

		# Create first block template on startup
		self.update_block()

	def get_new_extranonce1(self):
		'''Generates unique extranonce1 (e.g. for newly
		subscribed connection.'''
		log.debug("Getting Unique Extronance")
		return self.extranonce_counter.get_new_bin()

	def get_last_broadcast_args(self):
		'''Returns arguments for mining.notify
		from last known template.'''
		log.debug("Getting arguments needed for mining.notify")
		return self.last_template.broadcast_args

	def add_template(self, template, block_height):
		'''Adds new template to the registry.
		It also clean up templates which should
		not be used anymore.'''

		prevhash = template.prevhash_hex

		if prevhash in self.prevhashes.keys():
			new_block = False
		else:
			new_block = True
			self.prevhashes[prevhash] = []

		# Blocks sorted by prevhash, so it's easy to drop
		# them on blockchain update
		self.prevhashes[prevhash].append(template)

		# Weak reference for fast lookup using job_id
		self.jobs[template.job_id] = template

		# Use this template for every new request
		self.last_template = template

		# Drop templates of obsolete blocks
		for ph in self.prevhashes.keys():
			if ph != prevhash:
				del self.prevhashes[ph]

		log.info("New template for %s" % prevhash)

		if new_block:
			# Tell the system about new block
			# It is mostly important for share manager
			self.on_block_callback(prevhash, block_height)

		# Everything is ready, let's broadcast jobs!
		self.on_template_callback(new_block)

		#from twisted.internet import reactor
		#reactor.callLater(10, self.on_block_callback, new_block) 

	def update_block(self, force = False):
		'''Registry calls the getblocktemplate() RPC
		and build new block template.'''
		log.info("A block update has been requested.")

		if self.update_in_progress and force and not self.GBT_RPC_ATTEMPT is None:
			# Cancel current block update request (if any)
			log.warning("Forcing block update.")
			self.GBT_RPC_ATTEMPT.cancel()
			self.update_in_progress = False

		if self.update_in_progress:
			# Block has been already detected
			log.warning("Block update already in progress.  Started at: %s" % str(self.last_block_update_start_time))
			# It's possible for this process to get 'hung', lets see how long
			running_time = max(Interfaces.timestamper.time() - self.last_block_update_start_time , 0)
			log.info("Block update running for %i seconds" % running_time)
			# If it's been more than 30 seconds, then cancel it
			# But we don't run in this instance.
			if running_time >= 30:
				log.error("Block update appears to be hung.  Running for more than %i seconds.  Canceling..." % running_time)
				self.GBT_RPC_ATTEMPT.cancel()
				self.update_in_progress = False

			return

		log.debug("Block update started.")
		self.update_in_progress = True
		self.last_block_update_start_time = Interfaces.timestamper.time()

		# Polls the upstream network daemon for new block template
		# This is done asyncronisly
		self.GBT_RPC_ATTEMPT = self.bitcoin_rpc.getblocktemplate()
		log.debug("Block template request sent")
		self.GBT_RPC_ATTEMPT.addCallback(self._update_block)
		self.GBT_RPC_ATTEMPT.addErrback(self._update_block_failed)

	def _update_block_failed(self, failure):
		# Runs when upstream 'getblocktemplate()' RPC call has failed
		try:
			log.error("Could not load block template: %s" % str(failure))
		except:
			log.error("Could not load block template.")
		finally:
			self.update_in_progress = False

	def _update_block(self, data):
		# Runs when upstream 'getblocktemplate()' RPC call has completed with no error
		log.debug("Block template data recived: Creating new template...")
		# Generate a new template
		template = self.template_generator.new_template(Interfaces.timestamper, JobIdGenerator.get_new_id())
		# Apply newly obtained template 'data' as recived from upstream network
		log.debug("Filling RPC Data")
		template.fill_from_rpc(data)
		# Add it the template registry
		log.debug("Adding to Registry")
		self.add_template(template, data['height'])

		# All done
		log.info("Block update finished, %.03f sec, %d txes" % (Interfaces.timestamper.time() - self.last_block_update_start_time, len(template.block.vtx)))
		self.update_in_progress = False

		'''
		TODO: Investigate
		I don't think it is nessesary to return the raw block template data 
		or anything at all from the '_update_block' function since it's called asyncronosly. 

		Leaving it for now.
		'''
		return data

	def diff_to_target(self, difficulty):
		'''Converts difficulty to target'''
		return util.get_diff_target(difficulty);

	def get_job(self, job_id, worker_name, ip=False):
		'''For given job_id returns BlockTemplate instance or None'''
		try:
			j = self.jobs[job_id]
		except:
			log.info("Job id '%s' not found, worker_name: '%s'" % (job_id, worker_name))
			if ip:
				log.debug("Worker submited invalid Job id: IP %s", str(ip))

			return None

		# Now we have to check if job is still valid.
		# Unfortunately weak references are not bulletproof and
		# old reference can be found until next run of garbage collector.
		if j.prevhash_hex not in self.prevhashes:
			log.debug("Prevhash of job '%s' is unknown" % job_id)
			return None

		if j not in self.prevhashes[j.prevhash_hex]:
			log.debug("Job %s is unknown" % job_id)
			return None

		return j

	def submit_share(self, job_id, worker_name, session, extranonce1_bin, extranonce2, ntime, nonce, difficulty, ip=False):
		'''Check parameters and finalize block template. If it leads
			to valid block candidate, asynchronously submits the block
			back to the bitcoin network.

			- extranonce1_bin is binary. No checks performed, it should be from session data
			- job_id, extranonce2, ntime, nonce - in hex form sent by the client
			- difficulty - decimal number from session
			- submitblock_callback - reference to method which receive result of submitblock()
			- difficulty is checked to see if its lower than the vardiff minimum target or pool target
			  from conf/config.py and if it is the share is rejected due to it not meeting the requirements for a share
			  
		'''
		log.debug("Session: %s" % session)

		# Share Difficulty should never be 0 or below
		if difficulty <= 0 :
			log.exception("Worker %s @ IP: %s seems to be submitting Fake Shares"%(worker_name,ip))
			raise SubmitException("Diff is %s Share Rejected Reporting to Admin"%(difficulty))

		# Check if extranonce2 looks correctly. extranonce2 is in hex form...
		if len(extranonce2) != self.extranonce2_size * 2:
			raise SubmitException("Incorrect size of extranonce2. Expected %d chars" % (self.extranonce2_size*2))

		# Check for job
		job = self.get_job(job_id, worker_name, ip)
		if job == None:
			if settings.REJECT_STALE_SHARES:
				# Reject stale share
				raise SubmitException("Job '%s' not found" % job_id)
			else:
				# Accept stale share but do not continue checking, return a bunch of nothingness
				log.info("Accepted Stale Share from %s, (%s %s %s %s)" % \
					(worker_name, binascii.hexlify(extranonce1_bin), extranonce2, ntime, nonce))
				return (None, None, None, None, None, None)

		# Check if ntime looks correct
		check_result, error_message = util.check_ntime(ntime)
		if not check_result:
			raise SubmitException(error_message)

		if not job.check_ntime(int(ntime, 16), getattr(settings, 'NTIME_AGE')):
			raise SubmitException("Ntime out of range")

		# Check nonce
		check_result, error_message = util.check_nonce(nonce)
		if not check_result:
			raise SubmitException(error_message)

		# Check for duplicated submit
		if not job.register_submit(extranonce1_bin, extranonce2, ntime, nonce):
			log.info("Duplicate from %s, (%s %s %s %s)" % \
					(worker_name, binascii.hexlify(extranonce1_bin), extranonce2, ntime, nonce))
			raise SubmitException("Duplicate share")

		# Now let's do the hard work!
		# ---------------------------

		# 0. Some sugar
		extranonce2_bin = binascii.unhexlify(extranonce2)
		ntime_bin = util.get_ntime_bin(ntime)
		nonce_bin = util.get_nonce_bin(nonce)
		target_user = self.diff_to_target(difficulty)
		target_info = self.diff_to_target(100000)

		# 1. Build coinbase
		coinbase_bin = job.serialize_coinbase(extranonce1_bin, extranonce2_bin)
		coinbase_hash = util.get_coinbase_hash(coinbase_bin)

		# 2. Calculate merkle root
		merkle_root_bin = job.merkletree.withFirst(coinbase_hash)
		merkle_root_int = util.uint256_from_str(merkle_root_bin)

		# 3. Serialize header with given merkle, ntime and nonce
		header_bin = job.serialize_header(merkle_root_int, ntime_bin, nonce_bin)

		# 4. Convert header into hex according to hash algorythim
		block_hash = util.get_hash_hex(header_bin, ntime, nonce)

		# 5a Compare it with target of the user
		check_result, message = util.check_header_target(block_hash['int'], target_user)
		if not check_result:
			log.debug("Oops, somthing is wrong: target_user=%s, difficulty=%s, share_diff=%s" % (target_user, difficulty, int(self.diff_to_target(block_hash['int']))))
			raise SubmitException("%s. Hash: %s" % (message, block_hash['hex']))

		# Mostly for debugging purposes, just a celebratory message that's being carried over from older versions
		check_result, message = util.check_above_yay_target(block_hash['int'], target_info)
		if check_result:
			log.info(message)

		# Algebra tells us the diff_to_target is the same as hash_to_diff
		if settings.VDIFF_FLOAT:
			share_diff = float(self.diff_to_target(block_hash['int']))
		else:
			share_diff = int(self.diff_to_target(block_hash['int']))

		log.debug("share_diff: %s" % share_diff)
		log.debug("job.target: %s" % job.target)
		log.debug("block_hash_int: %s" % block_hash['int'])

		# 6. Compare hash with target of the network
		if util.is_block_candidate(block_hash['int'], job.target):
			# Yay! It is block candidate!
			log.info("We found a block candidate! for %i: %s | %s" % (job.height, block_hash['hex'], block_hash['check_hex']))

			# 7. Finalize and serialize block object 
			job.finalize(merkle_root_int, extranonce1_bin, extranonce2_bin, int(ntime, 16), int(nonce, 16))

			if not job.is_valid(difficulty):
				# Should not happen
				log.exception("FINAL JOB VALIDATION FAILED!(Try enabling/disabling tx messages)")

			# 8. Submit block to the network
			serialized = binascii.hexlify(job.serialize())
			on_submit = self.bitcoin_rpc.submitblock(serialized, block_hash['check_hex'], block_hash['hex'])

			if on_submit:
				self.update_block()

			return (block_hash['header_hex'], block_hash['solution_hex'], share_diff, job.prevhash_hex, job.height, on_submit)

		# Not a potential Block
		return (block_hash['header_hex'], block_hash['solution_hex'], share_diff, job.prevhash_hex, job.height, None)